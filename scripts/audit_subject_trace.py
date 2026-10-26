"""Read-only, evidence-preserving subject trace audit; semantic expectations stay in experiments.

Use --traces DIR --cases inputs.json [--cases ...] --expectations annotations.json
--output DIR. Existing legacy scores are copied unchanged. Exact fixture anchor matching
aligns a row to an explicitly annotated identity; descriptions never establish identity.
Missing or multiply matching anchors require manual review, rather than an identity score.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import unicodedata


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).casefold().split())


def fixture_target(owner, target, catalog):
    """Mirror the evaluator's documented fixture-ID mapping without importing runtime code."""
    original = next((row for row in catalog if row['id'] == target), {})
    parts = (owner, 'user' if original.get('is_user') else target)
    return 'obj_' + hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def names(row):
    """Only actual names align identities; role/species descriptions stay a separate dimension."""
    return {normalized(row.get('name', '')), *(
        normalized(item.get('name', '') if isinstance(item, dict) else item)
        for item in row.get('aliases', []))} - {''}


def is_user(row):
    return normalized(row.get('name', '')) in {'@user', 'user'}


def output_of(step):
    for key in ['normalized_output', 'effective_output', 'output', 'subject_output']:
        if key in step:
            value = step[key]
            if isinstance(value, str):
                try:
                    return json.loads(value), key, 'parsed_from_saved_string'
                except json.JSONDecodeError:
                    return None, key, 'invalid_json_string'
            return value, key, 'saved_parsed_value'
    return None, None, 'missing'


def legacy_dimensions(checks):
    dimensions = {}
    for check in checks:
        name = check['name']
        if name.endswith('_identity_and_names') or name == 'groups_remain_distinct':
            dimension = 'mixed_identity_names_descriptions'
        elif 'description' in name:
            dimension = 'description_or_search'
        elif 'alias' in name:
            dimension = 'name_or_alias'
        elif any(word in name for word in ['connection', 'navigation', 'edge']):
            dimension = 'connection_may_depend_on_mixed_alignment'
        else:
            dimension = 'other_original_check'
        dimensions.setdefault(dimension, []).append(check)
    return dimensions


def issue_list(step):
    issues = list(step.get('final_issues', []))
    for field in ['contract_errors', 'subject_errors', 'alias_issues', 'description_issues', 'connection_errors']:
        values = step.get(field, [])
        if isinstance(values, dict):
            issues.extend({'code': str(v), 'scope': field, 'ref_or_index': str(k)} for k, v in values.items())
        elif isinstance(values, list):
            issues.extend(v if isinstance(v, dict) else {'code': str(v), 'scope': field} for v in values)
    return issues


def audit_step(step, *, case=None, expectation=None, owner='', source_id=None):
    """Expose deterministic violations and explicitly annotated outcomes without semantic guessing."""
    case = case or {}
    expected = expectation or {}
    raw, selected_field, parse_status = output_of(step)
    report = {
        'source': step.get('source', case.get('payload', {}).get('new_user_source', {}).get('content', '')),
        'output_field': selected_field, 'parse_status': parse_status, 'findings': [],
        'identity_decisions': [], 'manual_review': [], 'normalization_notes': [], 'saved_program_issues': issue_list(step),
        'initial_issues': step.get('initial_issues', []), 'repair_audit': step.get('repair_audit', []),
        'annotation_basis': expected.get('basis', 'No semantic annotations; inspect original source and output.'),
        'manual_notes': expected.get('manual_notes', []),
    }
    findings = report['findings']
    def add(dimension, code, **evidence):
        findings.append(dict(dimension=dimension, code=code, **evidence))
    if not isinstance(raw, dict) or not isinstance(raw.get('subjects'), list) or not isinstance(raw.get('connections'), list):
        add('format', 'response_requires_subject_and_connection_arrays')
        report['manual_review'].append('No complete parsed subject/connection response is available.')
        return report
    subjects = raw['subjects']; connections = raw['connections']
    report['output'] = raw
    valid_rows = [row for row in subjects if isinstance(row, dict)]
    ref_counts = Counter(row.get('ref') for row in valid_rows)
    refs = set(ref_counts)
    for ref, count in ref_counts.items():
        if count > 1:
            add('protocol', 'duplicate_subject_ref', ref=ref, count=count)
    for index, connection in enumerate(connections):
        if not isinstance(connection, dict):
            add('format', 'connection_requires_object', index=index); continue
        endpoints = connection.get('subject_refs', [])
        if len(endpoints) != 2 or len(set(endpoints)) != 2:
            add('protocol', 'connection_requires_two_distinct_refs', index=index, endpoints=endpoints)
        elif any(ref not in refs for ref in endpoints):
            add('protocol', 'connection_ref_not_declared', index=index, endpoints=endpoints)
    payload = case.get('payload', {})
    primary = payload.get('new_user_source', {})
    current_id = source_id or primary.get('id')
    source = report['source']
    wire_output = step.get('wire_output', {})
    wire_rows = {row.get('ref'): row for row in wire_output.get('subjects', []) if isinstance(row, dict)} if isinstance(wire_output, dict) else {}
    for row in valid_rows:
        ref = row.get('ref'); operation = row.get('operation')
        if operation not in {'create', 'reuse', 'pending'}:
            add('format', 'unknown_operation', ref=ref, actual=operation)
        if operation in {'create', 'pending'} and row.get('target_id') is not None:
            add('protocol', 'new_or_pending_target_requires_null', ref=ref)
        if operation == 'reuse' and not row.get('target_id'):
            add('protocol', 'reuse_requires_target', ref=ref)
        reference = row.get('reference') or {}
        mention = row.get('mention') or reference.get('mention') or wire_rows.get(ref, {}).get('mention')
        if mention and mention not in source:
            if normalized(mention) not in normalized(source):
                add('current_reference', 'mention_not_in_current_source', ref=ref, mention=mention)
            else:
                report['normalization_notes'].append(dict(ref=ref, field='mention', text=mention,
                    note='Matches current source after the evaluator-compatible text normalization.'))
        if operation == 'reuse' and not mention:
            add('current_reference', 'reuse_missing_current_mention', ref=ref)
        for field, text_key, dimension in [('aliases', 'name', 'name'), ('descriptions', 'text', 'description')]:
            for value in row.get(field, []):
                if not isinstance(value, dict):
                    report['manual_review'].append(f'{field} for ref {ref} lacks normalized provenance.')
                    continue
                text = value.get(text_key, '')
                if current_id and value.get('source_id') != current_id:
                    add(dimension, 'addition_uses_non_current_source', ref=ref, text=text,
                        source_id=value.get('source_id'), expected_source_id=current_id)
                if text and text not in source:
                    if normalized(text) not in normalized(source):
                        add(dimension, 'addition_not_literal_in_current_source', ref=ref, text=text)
                    else:
                        report['normalization_notes'].append(dict(ref=ref, field=field, text=text,
                            note='Matches current source after the evaluator-compatible text normalization.'))
    catalog = payload.get('subject_catalog', [])
    identities = expected.get('identities', [])
    matched = {}; ambiguous_keys = set()
    for identity in identities:
        key = identity['key']; anchors = {normalized(value) for value in identity.get('names', [])}
        target = identity.get('target')
        expected_target = fixture_target(owner, target, catalog) if target else None
        candidates = [row for row in valid_rows if
            (expected_target is not None and row.get('target_id') in {expected_target, target}) or
            (anchors and names(row) & anchors) or
            (identity.get('is_user') is True and is_user(row))]
        # Repeated same-name identities use fixture-authored distinguishing display anchors when supplied.
        distinguish = {normalized(value) for value in identity.get('display_names', [])}
        if distinguish:
            candidates = [row for row in candidates if normalized(row.get('name', '')) in distinguish]
        if len(candidates) != 1:
            unscorable_refs = step.get('wire_identity_unscorable_refs', [])
            status = ('manual_review_protocol_invalid' if not candidates and unscorable_refs else
                      'omitted' if not candidates else 'manual_review')
            report['identity_decisions'].append(dict(key=key, status=status,
                candidate_refs=[row.get('ref') for row in candidates], expected=identity))
            if not candidates and unscorable_refs:
                ambiguous_keys.add(key)
                report['manual_review'].append(f'{key}: malformed wire identity fields on refs {unscorable_refs} prevent reliable name/ID alignment.')
            elif not candidates:
                add('identity_coverage', 'expected_identity_has_no_name_or_target_aligned_row', identity=key)
            else:
                ambiguous_keys.add(key)
                report['manual_review'].append(f'{key}: multiple rows share a name/target anchor; inspect current referents.')
            continue
        row = candidates[0]; matched[key] = row['ref']
        errors = []
        if identity.get('operation') and row.get('operation') != identity['operation']:
            errors.append('operation')
            add('identity_decision', 'operation_disagrees_with_explicit_expectation', identity=key,
                ref=row['ref'], expected=identity['operation'], actual=row.get('operation'))
        if expected_target is not None and row.get('target_id') not in {expected_target, target}:
            errors.append('target')
            add('identity_decision', 'selected_target_disagrees_with_explicit_expectation', identity=key,
                ref=row['ref'], expected_target=expected_target, actual_target=row.get('target_id'))
        if 'is_user' in identity and is_user(row) != identity['is_user']:
            errors.append('user')
            add('user_identity', 'user_role_disagrees_with_explicit_expectation', identity=key, ref=row['ref'])
        for term in identity.get('required_description_terms', []):
            descriptions = [normalized(value.get('text', '') if isinstance(value, dict) else value)
                            for value in row.get('descriptions', [])]
            if normalized(term) not in descriptions:
                add('description', 'description_term_exact_missing', identity=key, ref=row['ref'], term=term,
                    substring_present=any(normalized(term) in value for value in descriptions))
        for alias in identity.get('required_names', []):
            if normalized(alias) not in names(row):
                add('name', 'expected_name_missing_from_local_row', identity=key, ref=row['ref'], name=alias)
        report['identity_decisions'].append(dict(key=key, ref=row['ref'], status='decision_mismatch' if errors else 'matches_explicit_fields',
            expected=identity, actual_operation=row.get('operation'), actual_target=row.get('target_id')))
    assigned = Counter(matched.values())
    for ref, count in assigned.items():
        if count > 1:
            add('identity_coverage', 'several_expected_identities_share_one_row', ref=ref,
                identities=[key for key, value in matched.items() if value == ref])
    if expected.get('coverage_complete'):
        for row in valid_rows:
            if row.get('ref') not in assigned and not ambiguous_keys:
                add('identity_coverage', 'unexpected_identity_row', ref=row.get('ref'), name=row.get('name'))
    if expected.get('user_present') is False:
        for row in valid_rows:
            if is_user(row):
                add('user_identity', 'user_not_referred_to_in_current_source', ref=row.get('ref'))
    if 'expected_subject_count' in expected and len(valid_rows) != expected['expected_subject_count']:
        add('identity_coverage', 'subject_count_disagrees_with_explicit_expectation',
            expected=expected['expected_subject_count'], actual=len(valid_rows))
    pairs = {tuple(sorted(link.get('subject_refs', []))) for link in connections
             if isinstance(link, dict) and len(link.get('subject_refs', [])) == 2}
    for pair in expected.get('required_connections', []):
        if all(key in matched for key in pair):
            refs_pair = tuple(sorted(matched[key] for key in pair))
            if refs_pair not in pairs:
                add('connection', 'expected_navigation_pair_missing', identities=pair, refs=list(refs_pair))
        else:
            report['manual_review'].append(f'Connection {pair} cannot be scored before identity rows align.')
    for pair in expected.get('forbidden_connections', []):
        if all(key in matched for key in pair):
            refs_pair = tuple(sorted(matched[key] for key in pair))
            if refs_pair in pairs:
                add('connection', 'unsupported_navigation_pair', identities=pair, refs=list(refs_pair))
    if expected.get('connections_empty') and connections:
        add('connection', 'expected_no_navigation_connections', count=len(connections))
    if not identities and not expected.get('coverage_complete'):
        report['manual_review'].append('Identity correctness is unscored without explicit semantic annotations.')
    if not expected.get('semantic_review_complete'):
        report['manual_review'].append('Exact fields and literal evidence do not establish full semantic identity correctness.')
    report['dimension_finding_counts'] = dict(Counter(item['dimension'] for item in findings))
    return report


def _schema_ref(schema, root):
    if '$ref' not in schema:
        return schema
    value = root
    for part in schema['$ref'].removeprefix('#/').split('/'):
        value = value[part.replace('~1', '/').replace('~0', '~')]
    return value


def schema_findings(value, schema, root=None, path=()):
    """Validate the JSON Schema vocabulary used in the captured subject wire contracts."""
    root = schema if root is None else root
    schema = _schema_ref(schema, root)
    errors = []
    def fail(code, **details):
        errors.append(dict(path=list(path), code=code, **details))
    if 'anyOf' in schema:
        choices = [schema_findings(value, branch, root, path) for branch in schema['anyOf']]
        if not any(not choice for choice in choices):
            fail('any_of_mismatch', branch_errors=choices)
        return errors
    if 'const' in schema and value != schema['const']:
        fail('const_mismatch', expected=schema['const'])
    if 'enum' in schema and value not in schema['enum']:
        fail('enum_mismatch', expected=schema['enum'])
    expected_type = schema.get('type')
    matches = {
        'object': isinstance(value, dict), 'array': isinstance(value, list),
        'string': isinstance(value, str), 'integer': type(value) is int,
        'number': type(value) in {int, float}, 'null': value is None, 'boolean': type(value) is bool,
    }
    if expected_type and not matches.get(expected_type, False):
        fail('type_mismatch', expected=expected_type, actual=type(value).__name__)
        return errors
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        for key in schema.get('required', []):
            if key not in value:
                errors.append(dict(path=[*path, key], code='required_field_missing'))
        if schema.get('additionalProperties') is False:
            for key in value.keys() - properties.keys():
                errors.append(dict(path=[*path, key], code='extra_field'))
        for key in value.keys() & properties.keys():
            errors.extend(schema_findings(value[key], properties[key], root, (*path, key)))
    if isinstance(value, list):
        for key, compare in [('minItems', lambda v, bound: len(v) < bound), ('maxItems', lambda v, bound: len(v) > bound)]:
            if key in schema and compare(value, schema[key]):
                fail(key, expected=schema[key], actual=len(value))
        if 'items' in schema:
            for index, item in enumerate(value):
                errors.extend(schema_findings(item, schema['items'], root, (*path, index)))
    if isinstance(value, str):
        for key, compare in [('minLength', lambda v, bound: len(v) < bound), ('maxLength', lambda v, bound: len(v) > bound)]:
            if key in schema and compare(value, schema[key]):
                fail(key, expected=schema[key], actual=len(value))
    if type(value) in {int, float}:
        if 'minimum' in schema and value < schema['minimum']:
            fail('minimum', expected=schema['minimum'], actual=value)
        if 'maximum' in schema and value > schema['maximum']:
            fail('maximum', expected=schema['maximum'], actual=value)
    return errors


def _wire_shape_schema(schema):
    """Separate operation-dependent combinations from unconditional field shapes."""
    shape = json.loads(json.dumps(schema))
    for definition in shape.get('$defs', {}).values():
        branches = definition.get('anyOf', [])
        if not branches or not all('properties' in branch and 'operation' in branch['properties'] for branch in branches):
            continue
        choices = [branch['properties']['operation'].get('enum', []) for branch in branches]
        if not all(len(choice) == 1 and choice[0] in {'create', 'reuse', 'pending'} for choice in choices):
            continue
        merged = json.loads(json.dumps(branches[0]))
        merged['properties']['operation'] = dict(type='string', enum=[choice[0] for choice in choices])
        for field in ['target_id', 'label']:
            variants = []
            for branch in branches:
                candidate = branch['properties'][field]
                if candidate not in variants:
                    variants.append(candidate)
            merged['properties'][field] = dict(anyOf=variants)
        definition.clear(); definition.update(merged)
    return shape


def wire_contract(content, request):
    """Classify syntax, Schema shape and operation conditions from captured wire evidence."""
    result = dict(json_syntax_valid=True, schema_shape_valid=None, operation_conditions_valid=None,
                  protocol='unknown', findings=[])
    try:
        wire = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        result['json_syntax_valid'] = False
        result['findings'].append(dict(dimension='wire_contract', code='wire_json_syntax_error'))
        return result
    response_format = request.get('response_format', {}).get('json_schema', {})
    schema = response_format.get('schema')
    rows = wire.get('subjects', []) if isinstance(wire, dict) else []
    rows = rows if isinstance(rows, list) else []
    compact = response_format.get('name') == 'WirePlan' or any(isinstance(row, dict) and 'label' in row for row in rows)
    result['protocol'] = 'compact_label_wire' if compact else 'legacy_subject_wire'
    if schema:
        shapes = schema_findings(wire, _wire_shape_schema(schema))
        result['schema_shape_valid'] = not shapes
        result['strict_sent_schema_valid'] = not schema_findings(wire, schema)
        result['schema_shape_errors'] = shapes
        if shapes:
            result['findings'].append(dict(dimension='wire_contract', code='wire_schema_shape_error', errors=shapes))
    conditions = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        operation = row.get('operation')
        def error(field, code):
            conditions.append(dict(path=['subjects', index, field], ref=row.get('ref'), operation=operation, code=code))
        if operation in {'create', 'pending'}:
            if 'target_id' in row and row['target_id'] is not None:
                error('target_id', 'new_or_pending_target_requires_null')
            if compact and 'label' in row and not isinstance(row['label'], dict):
                error('label', 'new_or_pending_requires_label_object')
            if not compact and 'reference' in row and row['reference'] is not None:
                error('reference', 'new_or_pending_reference_requires_null')
        elif operation == 'reuse':
            if 'target_id' in row and (not isinstance(row['target_id'], str) or not row['target_id']):
                error('target_id', 'reuse_requires_target_id')
            if compact and 'label' in row and row['label'] is not None:
                error('label', 'reuse_requires_null_label')
            if not compact and 'reference' in row and not isinstance(row['reference'], dict):
                error('reference', 'reuse_requires_reference_object')
    result['operation_conditions_valid'] = not conditions
    result['operation_condition_errors'] = conditions
    if conditions:
        result['findings'].append(dict(dimension='wire_contract', code='wire_operation_condition_error', errors=conditions))
    return result


def request_fingerprint(request):
    keys = ['model', 'temperature', 'max_completion_tokens', 'max_tokens', 'messages', 'response_format', 'extra_body']
    body = {key: request[key] for key in keys if key in request}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def transport_index(trace_root):
    index = {}
    for path in sorted((Path(trace_root) / 'transport').rglob('*.json')):
        packet = json.loads(path.read_text())
        request = packet.get('request')
        if request:
            packet = dict(packet, trace_path=str(path.resolve()))
            index.setdefault(request_fingerprint(request), []).append(packet)
    return index


def raw_decision_step(step, case, packets, source_id=None, *, raw_attempt='last'):
    """Use model-owned decisions; candidate display/source metadata is deterministic audit decoration."""
    attempts = []
    for request in step.get('model_requests', []):
        matches = [packet for packet in packets.get(request_fingerprint(request), [])
                   if (packet.get('response') or {}).get('content')]
        if matches:
            packet = sorted(matches, key=lambda item: item.get('started_at', ''))[-1]
            content = packet['response']['content']
            try:
                wire = json.loads(content)
            except json.JSONDecodeError:
                wire = content
            attempts.append(dict(output=wire, transport_path=packet['trace_path'],
                matching_transport_packets=len(matches), request_sha256=request_fingerprint(request),
                wire_contract=wire_contract(content, request)))
    selected = dict(step)
    if not attempts:
        selected['decision_provenance'] = 'saved_normalized_model_proposal_without_transport'
        if 'output' in step:
            selected['normalized_output'] = step['output']
        return selected
    chosen_index = 0 if raw_attempt == 'first' else len(attempts)-1
    wire = attempts[chosen_index]['output']
    selected['selected_raw_attempt_index'] = chosen_index
    selected['wire_output'] = wire
    selected['raw_model_attempts'] = attempts
    selected['decision_provenance'] = 'transport_raw_model_reply'
    primary_id = source_id or case.get('payload', {}).get('new_user_source', {}).get('id')
    normalized_proposal = (step.get('attempt_outputs') or [step.get('output') or {}])[0] if raw_attempt == 'first' else step.get('output') or {}
    normalized_rows = {row['ref']: row for row in normalized_proposal.get('subjects', [])}
    if not isinstance(wire, dict) or not isinstance(wire.get('subjects'), list):
        selected['normalized_output'] = wire
        return selected
    decorated = dict(wire, subjects=[])
    delegated = set()
    compact_wire = attempts[chosen_index]['wire_contract']['protocol'] == 'compact_label_wire'
    selected['wire_identity_unscorable_refs'] = []
    for subject in wire['subjects']:
        if not isinstance(subject, dict):
            decorated['subjects'].append(subject); continue
        row = json.loads(json.dumps(subject))
        matching_row = normalized_rows.get(row.get('ref'), {})
        if compact_wire:
            label = row.get('label') or {}
            if not isinstance(label, dict):
                label = {}
            if (row.get('operation') in {'create', 'pending'} and
                    not (isinstance(label.get('name'), str) and label.get('name') and isinstance(label.get('kind'), str)) or
                    row.get('operation') == 'reuse' and not isinstance(row.get('target_id'), str)):
                selected['wire_identity_unscorable_refs'].append(row.get('ref'))
            row['name'] = label.get('name')
            row['kind'] = label.get('kind')
            if row.get('operation') == 'reuse':
                # The selected local E ID is preserved below; this decoration names its catalog entry.
                row['name'] = matching_row.get('name')
                row['kind'] = matching_row.get('kind')
                delegated.update(['reuse_display_name', 'reuse_kind'])
            for field, key in [('aliases', 'name'), ('descriptions', 'text')]:
                row[field] = [{key: value, 'source_id': primary_id} if isinstance(value, str) else value
                              for value in row.get(field, [])]
            row['reference'] = dict(mention=row.get('mention'), source_id=primary_id)
            delegated.update(['source_ids', 'historical_basis'])
        if not compact_wire:
            # Translate only local source labels, retaining each original field choice.
            for field, text_key in [('aliases', 'name'), ('descriptions', 'text')]:
                for value in row.get(field, []):
                    if isinstance(value, dict):
                        counterpart = next((stored for stored in matching_row.get(field, [])
                            if isinstance(stored, dict) and stored.get(text_key) == value.get(text_key)), None)
                        if counterpart:
                            value['source_id'] = counterpart.get('source_id', value.get('source_id'))
            raw_evidence = row.get('evidence', [])
            stored_evidence = matching_row.get('evidence', [])
            if len(raw_evidence) == len(stored_evidence):
                row['evidence'] = [dict(value, source_id=stored.get('source_id', value.get('source_id')))
                    for value, stored in zip(raw_evidence, stored_evidence)]
            if row.get('reference') and matching_row.get('reference'):
                row['reference'] = dict(row['reference'], source_id=matching_row['reference'].get('source_id'))
        raw_target = row.get('target_id')
        if raw_target and matching_row.get('operation') == row.get('operation') and matching_row.get('target_id'):
            # maintain maps local E labels to persistent IDs before returning its unreviewed proposal.
            row['raw_target_id'] = raw_target
            row['target_id'] = matching_row['target_id']
        decorated['subjects'].append(row)
    selected['normalized_output'] = decorated
    selected['delegated_metadata_fields'] = sorted(delegated)
    return selected


def provider_failure_type(error_type):
    return error_type if error_type in {'ProviderUnavailable', 'APIConnectionError', 'APITimeoutError',
        'ConnectTimeout', 'ReadTimeout', 'RateLimitError'} else None


def audit_trace(trace, *, case=None, expectation=None, decision_layer='effective', packets=None, raw_attempt='last'):
    steps = trace.get('steps')
    source_steps = steps if steps is not None else [trace]
    diagnostics = []
    if provider_failure_type(trace.get('error_type')) and not trace.get('output') and not steps:
        diagnostics.append(dict(source=(case or {}).get('payload', {}).get('new_user_source', {}).get('content', ''),
            output_field=None, parse_status='no_model_reply', findings=[], identity_decisions=[],
            manual_review=[], normalization_notes=[], saved_program_issues=[], initial_issues=[], repair_audit=[],
            provider_failure=dict(error_type=trace['error_type'], excluded_from_semantic_and_format_scoring=True)))
    else:
        for index, source_step in enumerate(source_steps):
            number = source_step.get('index', index)
            step_expectation = (expectation or {}).get('steps', {}).get(str(number)) if steps is not None else expectation
            step_case = {} if steps is not None else case or {}
            sid = f'step-{number}' if steps is not None else None
            selected = raw_decision_step(source_step, step_case, packets or {}, sid, raw_attempt=raw_attempt) if decision_layer == 'raw' else source_step
            diagnostic = audit_step(selected, case=step_case, expectation=step_expectation, owner=trace.get('id', ''), source_id=sid)
            contracts = [attempt['wire_contract'] for attempt in selected.get('raw_model_attempts', []) if 'wire_contract' in attempt]
            if contracts:
                chosen_contract = contracts[selected.get('selected_raw_attempt_index', len(contracts)-1)]
                diagnostic['selected_wire_contract'] = chosen_contract
                diagnostic['findings'].extend(chosen_contract['findings'])
                diagnostic['dimension_finding_counts'] = dict(Counter(item['dimension'] for item in diagnostic['findings']))
            diagnostic.update(decision_provenance=selected.get('decision_provenance', 'effective_program_view'),
                raw_model_attempts=selected.get('raw_model_attempts', []),
                delegated_metadata_fields=selected.get('delegated_metadata_fields', []),
                wire_identity_unscorable_refs=selected.get('wire_identity_unscorable_refs', []))
            diagnostics.append(diagnostic)
    for diagnostic, step in zip(diagnostics, source_steps):
        diagnostic['attempt_outputs'] = step.get('attempt_outputs', [])
        diagnostic['saved_wire_output'] = step.get('wire_output')
    return dict(id=trace.get('id'), legacy_passed=trace.get('passed'), legacy_checks=trace.get('checks', []),
        legacy_dimensions=legacy_dimensions(trace.get('checks', [])), diagnostics=diagnostics,
        provider_error_type=provider_failure_type(trace.get('error_type')), runtime_error_type=trace.get('error_type'), decision_layer=decision_layer, raw_attempt=raw_attempt,
        semantic_identity_accuracy=None,
        scoring_policy='Explicit fields and deterministic constraints are audited. Semantic identity accuracy requires adjudication.')


def audit_paths(trace_root, cases, expectations, decision_layer='effective', raw_attempt='last'):
    results = []
    packets = transport_index(trace_root) if decision_layer == 'raw' else {}
    for path in sorted(Path(trace_root).rglob('*.json')):
        try:
            trace = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(trace, dict) or 'passed' not in trace or 'id' not in trace:
            continue
        item = audit_trace(trace, case=cases.get(trace['id']), expectation=expectations.get(trace['id']),
                           decision_layer=decision_layer, packets=packets, raw_attempt=raw_attempt)
        item['trace_path'] = str(path.resolve()); results.append(item)
    counts = Counter(f['dimension'] for result in results for diag in result['diagnostics'] for f in diag['findings'])
    trace_dimensions = Counter(dimension for result in results for dimension in {
        finding['dimension'] for diag in result['diagnostics'] for finding in diag['findings']})
    first_outputs = [(diag.get('raw_model_attempts') or [{}])[0].get('output')
                     for result in results for diag in result['diagnostics']]
    first_counts = Counter(row.get('operation', 'unknown') for output in first_outputs if isinstance(output, dict)
        for row in output.get('subjects', []) if isinstance(row, dict))
    raw_counts = Counter(row.get('operation', 'unknown') for result in results for diag in result['diagnostics']
        if not diag.get('provider_failure') for row in diag.get('output', {}).get('subjects', []) if isinstance(row, dict))
    selected_contracts = [diag['selected_wire_contract'] for result in results for diag in result['diagnostics'] if 'selected_wire_contract' in diag]
    all_contracts = [attempt['wire_contract'] for result in results for diag in result['diagnostics']
                     for attempt in diag.get('raw_model_attempts', []) if 'wire_contract' in attempt]
    def contract_counts(contracts):
        return dict(replies=len(contracts), json_syntax_failures=sum(c['json_syntax_valid'] is False for c in contracts),
            schema_shape_failures=sum(c['schema_shape_valid'] is False for c in contracts),
            operation_condition_failures=sum(c['operation_conditions_valid'] is False for c in contracts))
    manual_statuses = Counter(decision['status'] for result in results for diag in result['diagnostics']
        for decision in diag['identity_decisions'])
    summary = dict(traces=len(results), legacy_passed=sum(result['legacy_passed'] is True for result in results),
        finding_counts=dict(counts), dimension_trace_counts=dict(trace_dimensions), annotated_traces=sum(result['id'] in expectations for result in results),
        manual_review_steps=sum(bool(diag['manual_review']) for result in results for diag in result['diagnostics']),
        manual_alignment_identities=manual_statuses.get('manual_review', 0),
        manual_protocol_invalid_identities=manual_statuses.get('manual_review_protocol_invalid', 0), explicit_field_status_counts=dict(manual_statuses),
        decision_layer=decision_layer, raw_attempt=raw_attempt, selected_model_operation_counts=dict(raw_counts), first_raw_reply_operation_counts=dict(first_counts),
        transport_raw_steps=sum(diag.get('decision_provenance') == 'transport_raw_model_reply' for result in results for diag in result['diagnostics']),
        provider_failures=[dict(id=result['id'], error_type=result['provider_error_type'], trace_path=result['trace_path']) for result in results if result['provider_error_type']],
        semantic_identity_accuracy=None, selected_wire_contract=contract_counts(selected_contracts),
        all_model_reply_wire_contract=contract_counts(all_contracts))
    return dict(summary=summary, traces=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--traces', type=Path, required=True)
    parser.add_argument('--cases', type=Path, action='append', default=[])
    parser.add_argument('--expectations', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--decision-layer', choices=['effective', 'raw'], default='effective')
    parser.add_argument('--raw-attempt', choices=['first', 'last'], default='last')
    args = parser.parse_args()
    cases = {}
    for path in args.cases:
        data = json.loads(path.read_text())
        cases.update({case['id']: case for case in data.get('cases', [])})
    expectations = json.loads(args.expectations.read_text()).get('cases', {}) if args.expectations else {}
    result = audit_paths(args.traces, cases, expectations, args.decision_layer, args.raw_attempt)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'summary.json').write_text(json.dumps(result['summary'], ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
