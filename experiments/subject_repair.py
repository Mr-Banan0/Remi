"""A bounded repair keeps validated decisions while correcting the failed fields."""
from collections import Counter
from copy import deepcopy
from experiments.subject_validation import issue


IDENTITY_FIELDS = ('ref', 'operation', 'target_id', 'name', 'kind', 'reference')


def build_repair_state(raw, reviewed, state):
    subjects = raw.get('subjects', []) if isinstance(raw, dict) else []
    subjects = subjects if isinstance(subjects, list) else []
    fixed = []; partial = []
    counts = Counter(r.get('ref') for r in subjects if isinstance(r, dict) and type(r.get('ref')) is int)
    for index, row in enumerate(subjects[:96]):
        if not isinstance(row, dict) or type(row.get('ref')) is not int or counts[row['ref']] != 1: continue
        own = [e for e in reviewed['issues'] if e['path'][:2] == ['subjects', index]]
        if not own:
            fixed.append(deepcopy(row)); continue
        if any(e['scope'] == 'identity' for e in own): continue
        fields = {k: deepcopy(row[k]) for k in IDENTITY_FIELDS}
        valid = {}
        for name in ['aliases', 'descriptions']:
            values = row[name] if isinstance(row[name], list) else []
            invalid = [e['path'] for e in own if len(e['path']) > 2 and e['path'][2] == name]
            valid[name] = [deepcopy(v) for n, v in enumerate(values[:24])
                           if not any(len(path) == 3 or path[3] == n for path in invalid)]
        partial.append(dict(ref=row['ref'], fields=fields, valid_additions=valid,
                            editable_fields=sorted({e['path'][2] for e in own if e['scope'] in {'alias','description'}})))
    mutable_refs={entry['ref'] for entry in reviewed['issues'] if entry['code'] in
                  {'subject_ref_selected_more_than_once','existing_identity_uses_one_local_ref'}}
    protected=[row for row in reviewed['protected_connections'] if not set(row['subject_refs']).intersection(mutable_refs)]
    allow_new=any(e['code']=='memory_participant_requires_subject_decision' for e in reviewed['issues']) or any(e['scope']=='contract' for e in reviewed['issues']) or (not subjects and any(e['scope']=='connection' for e in reviewed['issues']))
    return {**deepcopy(state), 'allow_new_subject_refs': allow_new, 'mutable_subject_refs': sorted(mutable_refs), 'previous_output': deepcopy(raw), 'repair_errors': deepcopy(reviewed['issues']),
            'fixed_subjects': fixed, 'fixed_subject_fields': partial,
            'protected_connections': deepcopy(protected)}


def merge_repair(repair_state, returned):
    """Preserve validated rows and stable refs; record attempted regressions separately."""
    previous = repair_state['previous_output']; audit = []
    original = previous.get('subjects') if isinstance(previous, dict) else None
    incoming = returned.get('subjects') if isinstance(returned, dict) else None
    incoming = incoming if isinstance(incoming, list) else []
    fixed = {row['ref']: row for row in repair_state['fixed_subjects']}
    partial = {row['ref']: row for row in repair_state['fixed_subject_fields']}
    by_ref = {}
    for row in incoming:
        if isinstance(row, dict) and type(row.get('ref')) is int: by_ref.setdefault(row['ref'], []).append(row)
    original_counts=Counter(row['ref'] for row in original or [] if isinstance(row,dict) and type(row.get('ref')) is int)
    flexible=repair_state['allow_new_subject_refs'] or any(n>1 for n in original_counts.values()) or any(not isinstance(row,dict) or type(row.get('ref')) is not int for row in original or [])
    original_refs = {row['ref'] for row in original or [] if isinstance(row, dict) and type(row.get('ref')) is int}
    previous_connections=previous.get('connections',[]) if isinstance(previous,dict) else []
    previous_connections=previous_connections if isinstance(previous_connections,list) else []
    connection_refs = {ref for row in previous_connections if isinstance(row, dict)
                       and isinstance(row.get('subject_refs'), list) for ref in row['subject_refs']
                       if type(ref) is int and 0 <= ref <= 95} if isinstance(previous, dict) else set()
    allowed = original_refs | connection_refs
    def notice(ref, code, message):
        audit.append(issue('repair', ['subjects'], code, message, ref=ref))
    complete=isinstance(returned,dict) and set(returned)=={'subjects','connections'} and all(isinstance(returned[k],list) for k in ['subjects','connections'])
    withdrawn=set();subjects = []
    if isinstance(original, list):
        seen=set()
        for row in original[:96]:
            if not isinstance(row, dict) or type(row.get('ref')) is not int:
                # A malformed row has no stable ref to preserve; the corrected response supplies it.
                continue
            ref = row['ref']
            if ref in seen:continue
            seen.add(ref)
            candidates = by_ref.get(ref, [])
            if ref in fixed:
                if candidates != [fixed[ref]]:
                    notice(ref, 'repair_restored_valid_subject', 'The program retained this validated subject and its additions.')
                subjects.append(deepcopy(fixed[ref])); continue
            if not candidates and complete and ref not in partial:
                withdrawn.add(ref)
                notice(ref,'repair_withdrew_unconfirmed_subject','The complete response withdrew this unconfirmed proposal; validated identities remain protected.')
                continue
            if not candidates:
                notice(ref, 'repair_retained_omitted_failed_subject', 'The omitted previous ref remains available for validation.')
                subjects.extend(deepcopy(r) for r in original if isinstance(r,dict) and r.get('ref')==ref)
                continue
            elif len(candidates) > 1:
                notice(ref, 'repair_duplicate_subject_ref', 'Repeated repaired rows keep the previous row unresolved.')
                chosen = deepcopy(row)
            else: chosen = deepcopy(candidates[0])
            if ref in partial:
                locked = partial[ref]
                if any(chosen.get(k) != v for k, v in locked['fields'].items()):
                    notice(ref, 'repair_restored_valid_identity', 'The program retained the validated identity fields.')
                chosen.update(deepcopy(locked['fields']))
                for field, valid in locked['valid_additions'].items():
                    if field not in locked['editable_fields']:
                        chosen[field]=deepcopy(row[field]);continue
                    additions = chosen.get(field, [])
                    additions = additions if isinstance(additions, list) else []
                    if any(item not in additions for item in valid):
                        notice(ref, 'repair_restored_valid_addition', 'The program retained validated names/descriptions.')
                    chosen[field] = [*deepcopy(valid), *[item for item in additions if item not in valid]]
            subjects.append(chosen)
        for ref, rows in by_ref.items():
            if ref in original_refs: continue
            if ref in allowed or flexible:
                subjects.extend(deepcopy(rows))
            else: notice(ref, 'repair_ignored_new_subject_ref', 'The response introduced a ref outside the original decisions.')
    else: subjects = deepcopy(incoming)
    protected=[row for row in repair_state['protected_connections'] if not set(row['subject_refs']).intersection(withdrawn)]
    if len(protected)!=len(repair_state['protected_connections']):
        audit.append(issue('repair',['connections'],'repair_withdrew_unresolved_connection','Removed pending connections whose unconfirmed endpoint was withdrawn.'))
    connections = deepcopy(protected)
    protected_pairs = {tuple(sorted(row['subject_refs'])) for row in protected}
    incoming_connections = returned.get('connections', []) if isinstance(returned, dict) else []
    incoming_connections = incoming_connections if isinstance(incoming_connections, list) else []
    for row in incoming_connections:
        refs = row.get('subject_refs') if isinstance(row, dict) else None
        if isinstance(refs,list) and all(type(ref) is int for ref in refs) and set(refs).intersection(withdrawn):
            audit.append(issue('repair',['connections'],'repair_removed_withdrawn_endpoint_connection','Rejected a repaired connection to a withdrawn unconfirmed ref.'))
            continue
        if isinstance(refs, list) and len(refs) == 2 and all(type(ref) is int for ref in refs) and tuple(sorted(refs)) in protected_pairs:
            continue
        connections.append(deepcopy(row))
    missing = [row for row in protected if row not in incoming_connections]
    if missing:
        audit.append(issue('repair', ['connections'], 'repair_restored_protected_connections',
                           'The program retained validated connections, including those waiting for corrected identities.'))
    return dict(subjects=subjects, connections=connections), audit


def finish_repair(repair_state, returned, state, store):
    from experiments.subject_connections import review
    effective, audit = merge_repair(repair_state, returned)
    reviewed = review(effective, state, store)
    # A malformed envelope or extra fields remain visible even when validated siblings survive.
    if not isinstance(returned, dict) or set(returned) != {'subjects', 'connections'} or any(
            not isinstance(returned.get(k), list) for k in ['subjects', 'connections']):
        reviewed['issues'].append(issue('contract', [], 'repair_requires_complete_response',
            'Return the complete subjects/connections response using the original contract.'))
        reviewed['contract_errors'].append('repair_requires_complete_response')
    reviewed.update(repair_audit=audit, effective_output=effective,
                    repair_before_issue_count=len(repair_state['repair_errors']))
    return reviewed
