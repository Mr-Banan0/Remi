"""Resolve each source-grounded identity once and validate its alias additions separately."""
from collections import Counter

from .clock import parse_time
from .identity import normalized_name, source_name_present, stable_id, visible_entity
from .models import AliasEvidence, GraphObject, Subject, SubjectDecision, SubjectDraft, SubjectNameDraft, SubjectNameEvidence, EvidenceRef


def decode_subjects(raw, state, store):
    state['proposal'] = {'subjects': [], 'items': [], 'reason': 'direct_original_subject_maintenance'}
    state['subject_alias_issues'] = []
    result, errors = {}, {}
    if (not isinstance(raw, dict) or set(raw) != {'subjects', 'connections'}
            or any(not isinstance(raw[k], list) for k in raw)):
        return {}, {-1: 'subject_plan_requires_subjects_connections'}
    if len(raw['subjects']) > 96 or len(raw['connections']) > 48:
        return {}, {-1: 'direct_subject_plan_exceeds_bounded_work'}
    sources = state['source_catalog']
    catalog = {o['id']: o for o in state['subject_catalog']}
    rows = raw['subjects']
    counts = Counter(r.get('ref') for r in rows if isinstance(r, dict) and type(r.get('ref')) is int)
    targets = Counter(r.get('target_id') for r in rows if isinstance(r, dict)
                      and r.get('operation') == 'reuse' and isinstance(r.get('target_id'), str))

    def original_evidence(citations):
        resolved = {}
        for citation in citations:
            source = sources.get(citation.source_id)
            if source is None or source.get('owner', state['owner']) != state['owner']:
                raise ValueError('subject_source_requires_supplied_original')
            resolved[citation.source_id] = EvidenceRef(source_id=citation.source_id, quote=source['content'])
        return list(resolved.values())

    for index, row in enumerate(rows):
        ref = row.get('ref', -index - 1) if isinstance(row, dict) else -index - 1
        if type(ref) is not int:
            ref = -index - 1
        try:
            # Core identity can succeed while individual proposed aliases are rejected.
            core = {**row, 'aliases': []} if isinstance(row, dict) and 'aliases' in row else row
            draft = SubjectDraft.model_validate(core)
            ref = draft.ref
            if counts[ref] != 1:
                raise ValueError('subject_ref_selected_more_than_once')
            evidence = original_evidence(draft.evidence)
            marker = draft.name == '@user'
            if marker and not any(sources[e.source_id]['role'] == 'user' for e in evidence):
                raise ValueError('speaker_identity_requires_user_source')
            key, old, known_names = None, None, []
            if draft.operation == 'reuse':
                old = catalog.get(draft.target_id)
                current = store.get('objects', state['owner'], draft.target_id) if old is not None else None
                clock = state['new_user_source']['message_at'] or state['new_user_source']['created_at']
                visible = visible_entity(GraphObject.model_validate(current), clock) if current else None
                if old is None or visible is None:
                    raise ValueError('reuse_requires_visible_owned_subject')
                if targets[draft.target_id] != 1:
                    raise ValueError('existing_identity_uses_one_local_ref')
                if old['kind'] != draft.kind:
                    raise ValueError('reuse_requires_same_subject_kind')
                if marker and not old['is_user']:
                    raise ValueError('speaker_marker_requires_canonical_person_identity')
                known_names = [old['name'], *[a.name for a in visible.aliases]]
                key = draft.target_id
            elif draft.operation == 'create':
                if draft.target_id is not None:
                    raise ValueError('create_assigns_id_by_program')
                key = (stable_id('obj_', state['owner'], 'user') if marker else
                       stable_id('obj_', state['owner'], state['new_user_source']['id'], state.get('source_window'), ref))
            elif draft.target_id is not None:
                raise ValueError('pending_subject_preserves_existing_records')
            is_user = old['is_user'] if old else marker
            if is_user and (draft.kind != 'person' or key is not None and key != stable_id('obj_', state['owner'], 'user')):
                raise ValueError('speaker_marker_requires_canonical_person_identity')

            aliases = []
            proposed = row['aliases']
            if not isinstance(proposed, list):
                state['subject_alias_issues'].append({'ref': ref, 'error': 'subject_aliases_require_list'})
                proposed = []
            if len(proposed) > 24:
                state['subject_alias_issues'].append({'ref': ref, 'error': 'subject_aliases_exceed_bounded_work'})
            for alias_index, value in enumerate(proposed[:24]):
                try:
                    alias = SubjectNameDraft.model_validate(value)
                    support = original_evidence([alias])[0]
                    if not source_name_present(normalized_name(alias.name), normalized_name(support.quote)):
                        raise ValueError('alias_requires_original_name')
                    aliases.append(SubjectNameEvidence(name=alias.name, source_id=support.source_id, quote=support.quote))
                except (ValueError, TypeError, AttributeError) as error:
                    state['subject_alias_issues'].append({'ref': ref, 'alias_index': alias_index,
                                                         'error': str(error).split('\n')[0][:180]})
            if old:
                allowed = {normalized_name(n) for n in [*known_names, *[a.name for a in aliases]]}
                if not marker and normalized_name(draft.name) not in allowed:
                    raise ValueError('reuse_name_requires_selected_identity')
                support_names = known_names
            else:
                support_names = [draft.name]
            if not marker and not any(source_name_present(normalized_name(name), normalized_name(e.quote))
                                      for name in support_names for e in evidence):
                raise ValueError('subject_identity_requires_original_name')
            # The fixed view carries recalled names so later memory extraction can follow an alias.
            view_names = [*[a['name'] for a in old.get('aliases', [])], old['name']] if old else []
            labels = list(dict.fromkeys([*view_names, *[a.name for a in aliases]]))
            resolved_evidence = {e.source_id: e for e in [*evidence, *aliases]}
            subject = Subject(ref=ref, name=draft.name, kind=draft.kind, is_user=is_user,
                              aliases=labels,
                              evidence=[e.model_dump(include={'source_id', 'quote'}) for e in resolved_evidence.values()])
            state['proposal']['subjects'].append(subject.model_dump())
            result[ref] = SubjectDecision(subject_ref=ref, target_id=key, kind=draft.kind,
                alias_additions=aliases,
                reason='explicit_pending' if key is None else 'source_grounded_subject_identity')
        except (ValueError, TypeError, AttributeError) as error:
            errors[ref] = str(error).split('\n')[0][:180]
            if 0 <= ref <= 95 and ref not in result and isinstance(row, dict):
                name = row.get('name', 'unresolved')
                kind = row.get('kind', 'person')
                if not isinstance(kind, str) or kind not in {'person', 'animal', 'organization'}:
                    kind = 'person'
                state['proposal']['subjects'].append(Subject(ref=ref, name=str(name)[:200], kind=kind,
                    is_user=False, aliases=[], evidence=[]).model_dump())
                result[ref] = SubjectDecision(subject_ref=ref, target_id=None, kind=kind,
                                             reason=errors[ref])
            if ref in result:
                result[ref].target_id = None
    return result, errors


def materialize_subjects(state, decisions, store):
    owner = state['owner']
    primary = state['new_user_source']
    observed = primary['message_at'] or primary['created_at']
    rows, ids, expected, issues = {}, {}, {}, []
    for raw in state['proposal']['subjects']:
        subject = Subject.model_validate(raw)
        decision = decisions[subject.ref]
        if decision.target_id is None:
            issues.append({'subject_ref': subject.ref, 'reason': decision.reason})
            continue
        key = decision.target_id
        current = store.get('objects', owner, key)
        expected.setdefault(key, current)
        is_user = key == stable_id('obj_', owner, 'user')
        existing = rows.get(key) or current
        obj = GraphObject.model_validate(existing) if existing else GraphObject(
            id=key, owner=owner, name='user' if is_user else subject.name, is_user=is_user,
            kind=decision.kind, observed_at=observed)
        known = {}
        for alias in obj.aliases:
            label, stamp = normalized_name(alias.name), parse_time(alias.observed_at)
            known[label] = min(known.get(label, stamp), stamp)
        if existing:
            label, stamp = normalized_name(obj.name), parse_time(obj.observed_at)
            known[label] = min(known.get(label, stamp), stamp)
        obj.observed_at = min([obj.observed_at, observed], key=parse_time)
        additions = [(a.name, a) for a in decision.alias_additions]
        if subject.name != '@user':
            support = next((e for e in subject.evidence
                            if source_name_present(normalized_name(subject.name), normalized_name(e.quote))), None)
            if support is not None:
                additions.insert(0, (subject.name, support))
        for name, support in additions:
            source = state['source_catalog'][support.source_id]
            alias_at = source.get('message_at') or source['created_at']
            label, stamp = normalized_name(name), parse_time(alias_at)
            if label in known and known[label] <= stamp:
                continue
            obj.aliases.append(AliasEvidence(name=name, source_id=support.source_id, quote=support.quote,
                                             observed_at=alias_at))
            known[label] = stamp
        rows[key] = obj.model_dump()
        ids[subject.ref] = key
    return {'subject_ids': {str(k): v for k, v in ids.items()}, 'entity_rows': list(rows.values()),
            'expected_objects': expected,
            'candidate_entity_ids': {c['id']: list(dict.fromkeys(ids[r] for r in c['subject_refs'] if r in ids))
                                     for c in state['proposal']['items']},
            'identity_dispositions': [{'candidate_id': c['id'], **issue} for c in state['proposal']['items']
                                      for issue in issues if issue['subject_ref'] in c['subject_refs']]}
