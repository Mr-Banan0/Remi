"""Propose, recall and jointly maintain one version-grounded memory graph."""
from __future__ import annotations

import json
from copy import deepcopy
import re
from time import monotonic

from pydantic import ValidationError

from .clock import absolute_content, parse_time, resolve_time, utcnow
from .identity import entity_candidates, entity_kind, normalized_name, stable_id
from .graph import project_edges
from .heat import visible_memory
from .models import (AddResponse, AliasEvidence, BoundMemoryLink, EvidenceRef, EntityRole, GraphObject, LiteralAttribute, MaintenancePlan, Memory,
                     ProposalPlan, Revision)
from .providers import OutputInvalid, ProviderUnavailable

PIPELINE = 'G08-propose-maintain-graph'


class PlanIssue(ValueError):
    def __init__(self, reason, details):
        super().__init__(reason)
        self.details = details


class AddPipeline:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    @staticmethod
    def chunks(text, size=12000):
        start = 0
        while start < len(text):
            stop = min(len(text), start + size)
            if stop < len(text):
                boundary = max(text.rfind('\n', start + size // 2, stop), text.rfind('. ', start + size // 2, stop))
                if boundary > start:
                    stop = boundary + 1
            yield start, stop, text[start:stop]
            start = stop

    @staticmethod
    def _source_evidence(raw, sources, groups):
        """Bind selected source IDs to program-copied originals in the supplied window."""
        result = deepcopy(raw)
        for group in groups:
            for item in result.get(group, []):
                evidence = []
                for citation in item.get('evidence', []):
                    source = sources.get(citation.get('source_id'))
                    if not source:
                        raise ValueError('evidence_requires_supplied_source')
                    evidence.append({'source_id': source['id'], 'quote': source['content']})
                item['evidence'] = evidence
        return result

    def _quote(self, evidence, sources):
        source = sources.get(evidence.source_id)
        if source is None or evidence.quote not in source['content']:
            raise ValueError('evidence_requires_supplied_source')
        return evidence

    def _proposals(self, raw, state):
        sources = {x['id']: x for x in [state['new_user_source'], *state['context_sources']]}
        plan = ProposalPlan.model_validate(self._source_evidence(raw, sources, ['items']))
        ids = set()
        accepted = []
        for item in plan.items:
            if item.id in ids:
                raise ValueError('candidate_ids_unique')
            ids.add(item.id)
            if state['new_user_source']['id'] not in {e.source_id for e in item.evidence}:
                continue
            item.evidence = [self._quote(e, sources) for e in item.evidence]
            accepted.append(item)
        if plan.items and not accepted:
            raise ValueError('candidate_requires_new_user_evidence')
        plan.items = accepted
        return plan

    def _validated_plan(self, raw, state):
        primary = state['new_user_source']
        sources = {x['id']: x for x in [primary, *state['context_sources'], *state['raw_history']]}
        for item in state['history']:
            sources.update({s['id']: s for s in item['sources']})
        raw = deepcopy(raw)
        candidates = {item['id']: item for item in state['proposal']['items']}
        for edit in raw.get('edits', []):
            references = list(edit.get('evidence', []))
            known_sources = {item['source_id'] for item in references}
            for candidate_id in edit.get('candidate_ids', []):
                for item in candidates.get(candidate_id, {}).get('evidence', []):
                    if item['source_id'] not in known_sources:
                        references.append({'source_id': item['source_id']})
                        known_sources.add(item['source_id'])
            edit['evidence'] = references
        used_refs = {normalized_name(r['entity_ref']) for e in raw.get('edits', []) for r in e.get('roles', [])}
        used_refs.update(normalized_name(a[k]) for e in raw.get('edits', []) for a in e.get('assertions', []) for k in ('subject_ref', 'object_ref'))
        raw = {**raw, 'entities': [obj for obj in raw.get('entities', []) if used_refs.intersection(normalized_name(name) for name in [obj['ref'], obj['name'], *obj.get('aliases', []), *([obj['target_id']] if obj.get('target_id') else []), *([state['owner']] if obj.get('is_self') else [])])]}
        plan = MaintenancePlan.model_validate(self._source_evidence(raw, sources, ['entities', 'edits']))
        scalar_refs = {obj.ref for obj in plan.entities if entity_kind(obj.kind) == 'attribute'}
        plan.entities = [obj for obj in plan.entities if obj.ref not in scalar_refs]
        for obj in plan.entities:
            obj.kind = entity_kind(obj.kind)
        for edit in plan.edits:
            edit.roles = [r for r in edit.roles if r.entity_ref not in scalar_refs]
            edit.assertions = [a for a in edit.assertions if a.subject_ref not in scalar_refs and a.object_ref not in scalar_refs]
        primary = state['new_user_source']
        sources = {x['id']: x for x in [primary, *state['context_sources'], *state['raw_history']]}
        history = {x['id']: x for x in state['history']}
        entities = {x['id']: x for x in state['candidate_entities']}
        for item in history.values():
            sources.update({s['id']: s for s in item['sources']})
        expected = {x['id'] for x in state['proposal']['items']}
        covered = {i for edit in plan.edits for i in edit.candidate_ids}
        covered.update(x.candidate_id for x in plan.discarded)
        if covered != expected:
            raise PlanIssue('every_candidate_requires_supported_edit_or_explicit_disposition', {'missing_candidate_ids': sorted(expected - covered), 'unexpected_candidate_ids': sorted(covered - expected), 'valid_candidate_ids': sorted(expected)})
        refs = {edit.ref for edit in plan.edits}
        if len(refs) != len(plan.edits):
            raise ValueError('memory_edit_refs_unique')
        groups = {}
        for obj in plan.entities:
            groups.setdefault(obj.ref, []).append(obj)
        unique_entities = []
        for ref, items in groups.items():
            signatures = {(normalized_name(o.name), o.kind, o.is_self, o.target_id) for o in items}
            if len(signatures) > 1:
                plan._graph_dispositions.append({'entity_ref': ref, 'reason': 'ambiguous_entity_ref'})
                continue
            merged = items[0]
            merged.aliases = list(dict.fromkeys(alias for item in items for alias in item.aliases))[:12]
            unique_entities.append(merged)
        plan.entities = unique_entities
        entity_refs = {obj.ref for obj in plan.entities}
        invalid_entities = set()
        for obj in plan.entities:
            obj.evidence = [self._quote(e, sources) for e in obj.evidence]
            basis = '\n'.join(e.quote for e in obj.evidence)
            if obj.target_id == obj.ref and obj.target_id not in entities:
                obj.target_id = None
            if obj.target_id is None and not obj.is_self:
                matches = [item['id'] for item in entities.values() if not item['is_self'] and entity_kind(item['kind']) == obj.kind and normalized_name(obj.name) in {normalized_name(n) for n in [item['name'], *item['aliases']]}]
                if len(matches) == 1:
                    obj.target_id = matches[0]
            existing = entities.get(obj.target_id)
            if existing and not obj.is_self and not existing['is_self']:
                old_names = {normalized_name(n) for n in [existing['name'], *existing['aliases']]}
                linked_name = normalized_name(obj.name) in old_names or any(name in normalized_name(basis) for name in old_names)
                if entity_kind(existing['kind']) != obj.kind or not linked_name:
                    plan._graph_dispositions.append({'entity_ref': obj.ref, 'proposed_target': obj.target_id, 'reason': 'entity_identity_requires_shared_name_or_alias_evidence'})
                    obj.target_id = None
                    existing = None
            # Named third parties keep their own identity; user self uses a program-owned canonical name.
            self_names = {'self', 'i', 'me', 'myself', '我', '本人'}
            if obj.is_self and normalized_name(obj.name) not in self_names:
                obj.is_self = False
            if obj.is_self:
                own_id = stable_id('obj_', state['owner'], 'self')
                obj.target_id = own_id if own_id in entities else None
                existing = entities.get(obj.target_id)
            if obj.target_id and obj.target_id not in entities:
                raise ValueError('entity_target_requires_recalled_identity')
            if existing and existing['is_self'] != obj.is_self:
                raise ValueError('entity_self_identity_mismatch')
            if obj.is_self:
                user_basis = '\n'.join(e.quote for e in obj.evidence if sources[e.source_id]['role'] == 'user')
                if not re.search(r'\b(I|me|my|myself)\b|我', user_basis, re.I) and not existing:
                    raise ValueError('self_requires_first_person_evidence')
            elif not existing and obj.kind in {'person', 'place', 'organization', 'project', 'document'} and normalized_name(obj.name) not in normalized_name(basis):
                invalid_entities.add(obj.ref)
                plan._graph_dispositions.append({'entity_ref': obj.ref, 'reason': 'new_entity_name_requires_source_mention'})
                continue
            # Only source-grounded surface names become new alias evidence. Existing names remain available on the identity.
            obj.aliases = [alias for alias in obj.aliases if normalized_name(alias) in normalized_name(basis)]
        plan.entities = [obj for obj in plan.entities if obj.ref not in invalid_entities]
        aliases = {}
        for obj in plan.entities:
            for name in [obj.ref, obj.name, *obj.aliases, *([obj.target_id] if obj.target_id else []), *([state['owner']] if obj.is_self else [])]:
                aliases.setdefault(normalized_name(name), set()).add(obj.ref)
        entity_refs = {obj.ref for obj in plan.entities}
        def resolve_ref(value):
            if value in entity_refs:
                return value
            matches = aliases.get(normalized_name(value), set())
            return next(iter(matches)) if len(matches) == 1 else value
        for edit in plan.edits:
            grounded_roles = []
            for role in edit.roles:
                role.entity_ref = resolve_ref(role.entity_ref)
                if role.entity_ref not in entity_refs:
                    plan._graph_dispositions.append({'edit_ref': edit.ref, 'role': role.model_dump(), 'reason': 'undeclared_role_entity'})
                    continue
                grounded_roles.append(role)
            edit.roles = grounded_roles
            grounded_assertions = []
            for assertion in edit.assertions:
                assertion.subject_ref = resolve_ref(assertion.subject_ref)
                if assertion.subject_ref not in entity_refs:
                    plan._graph_dispositions.append({'edit_ref': edit.ref, 'assertion': assertion.model_dump(), 'reason': 'undeclared_assertion_subject'})
                    continue
                resolved = resolve_ref(assertion.object_ref)
                if resolved in entity_refs:
                    assertion.object_ref = resolved
                else:
                    value = assertion.object_ref.removeprefix('literal:')
                    basis = normalized_name(' '.join(e.quote for e in edit.evidence))
                    simple = re.sub(r'(?<=\d)\s*(?:am|pm)$', '', normalized_name(value))
                    if normalized_name(value) not in basis and simple not in basis:
                        plan._graph_dispositions.append({'edit_ref': edit.ref, 'assertion': assertion.model_dump(), 'reason': 'ungrounded_object'})
                        continue
                    assertion.object_ref = 'literal:' + value
                if assertion.subject_ref == assertion.object_ref:
                    plan._graph_dispositions.append({'edit_ref': edit.ref, 'assertion': assertion.model_dump(), 'reason': 'reflexive_relation_in_content'})
                    continue
                grounded_assertions.append(assertion)
            edit.assertions = grounded_assertions
        # Check model-proposed update identities against the open relationship signature.
        # A declared independent relation gets its own identity; a unique exact signature can reuse history.
        declared = {obj.ref: obj for obj in plan.entities}
        def subject_id(ref):
            obj = declared.get(ref)
            if obj is None:
                return None
            return obj.target_id or (stable_id('obj_', state['owner'], 'self') if obj.is_self and normalized_name(obj.name) in {'self', 'i', 'me', 'myself', '我', '本人'} else None)
        def prior_signature(card):
            assertions = card.get('assertions', [])
            return (assertions[0]['subject_ref'], normalized_name(assertions[0]['predicate'])) if assertions else None
        for edit in plan.edits:
            if 'event' in edit.tags and edit.status in {'planned', 'occurred', 'cancelled'}:
                edit.temporal_scope = 'episodic'
            prior = history.get(edit.target_id)
            if prior and edit.operation == 'write' and prior['status'] == 'occurred' and prior['event_at'] and edit.status == 'occurred' and edit.temporal_scope == 'episodic' and not edit.corrects_occurrence:
                current_quotes = '\n'.join(e.quote for e in edit.evidence if e.source_id == primary['id'])
                stamp = resolve_time(edit.time_expression, current_quotes, primary['message_at'])
                if stamp and parse_time(stamp).date() != parse_time(prior['event_at']).date():
                    edit.target_id = None
                    continue
            if edit.operation != 'write' or not prior or prior['temporal_scope'] != 'ongoing' or not prior['assertions']:
                continue
            signatures = {(subject_id(a.subject_ref), normalized_name(a.predicate)) for a in edit.assertions}
            if prior_signature(prior) not in signatures:
                matches = [h['id'] for h in history.values() if h['temporal_scope'] == 'ongoing'
                           and prior_signature(h) is not None and prior_signature(h) in signatures]
                edit.target_id = matches[0] if len(matches) == 1 else None
        groups = {}
        for edit in plan.edits:
            if edit.target_id and edit.operation != 'keep':
                groups.setdefault(edit.target_id, []).append(edit)
        composed, redirects, removed = {}, {}, set()
        for target, items in groups.items():
            if len(items) < 2:
                continue
            signatures = {(e.operation, e.status, e.temporal_scope, e.time_expression, e.end_expression) for e in items}
            if len(signatures) != 1 or items[0].operation != 'write':
                raise ValueError('combine_edits_of_same_memory_target')
            record = items[0].model_dump()
            record['content'] = '\n\n'.join(dict.fromkeys(e.content for e in items))
            for field in ['candidate_ids', 'tags', 'evidence', 'roles', 'assertions', 'links']:
                values = [value for edit in items for value in edit.model_dump()[field]]
                record[field] = list({json.dumps(value, sort_keys=True, ensure_ascii=False): value for value in values}.values())
            record['change'] = 'replace' if any(e.change == 'replace' for e in items) else 'supplement' if any(e.change == 'supplement' for e in items) else 'confirm'
            record['corrects_occurrence'] = any(e.corrects_occurrence for e in items)
            record['impact'] = 'high' if any(e.impact == 'high' for e in items) else 'unknown' if any(e.impact == 'unknown' for e in items) else 'low'
            composed[items[0].ref] = type(items[0]).model_validate(record)
            for item in items[1:]:
                redirects[item.ref] = items[0].ref
                removed.add(item.ref)
            plan._write_compositions.append({'target_id': target, 'edit_refs': [e.ref for e in items], 'accepted_ref': items[0].ref, 'reason': 'compatible_same_target_supplements'})
        plan.edits = [composed.get(edit.ref, edit) for edit in plan.edits if edit.ref not in removed]
        refs = {edit.ref for edit in plan.edits}
        for edit in plan.edits:
            for link in edit.links:
                link.target_ref = redirects.get(link.target_ref, link.target_ref)
        for edit in plan.edits:
            if edit.target_id is not None and edit.target_id not in history:
                raise ValueError('memory_target_requires_recalled_history')
            prior = history.get(edit.target_id)
            if prior and edit.operation == 'write' and prior['temporal_scope'] == 'ongoing' and prior['assertions']:
                before = prior['assertions'][0]
                after = next((a for a in edit.assertions if normalized_name(a.predicate) == normalized_name(before['predicate'])), None)
                if after and not after.object_ref.startswith('literal:'):
                    obj = declared.get(after.object_ref)
                    if obj is None:
                        raise ValueError('assertion_requires_declared_endpoints')
                    object_id = obj.target_id or (stable_id('obj_', state['owner'], 'self') if obj.is_self else stable_id('obj_', state['owner'], primary['id'], obj.ref, obj.name))
                    if object_id != before['object_ref'] or after.negated != before.get('negated', False):
                        edit.change = 'replace'
            edit.evidence = [self._quote(e, sources) for e in edit.evidence]
            if primary['id'] not in {e.source_id for e in edit.evidence}:
                raise ValueError('edit_requires_new_user_evidence')
            if any(role.entity_ref not in entity_refs for role in edit.roles):
                raise ValueError('role_requires_declared_entity')
            if any(a.subject_ref not in entity_refs or (a.object_ref not in entity_refs and not a.object_ref.startswith('literal:')) for a in edit.assertions):
                raise ValueError('assertion_requires_declared_endpoints')
            grounded_links = []
            for link in edit.links:
                if link.target_ref in {edit.ref, edit.target_id} or link.target_ref not in refs | history.keys():
                    plan._graph_dispositions.append({'edit_ref': edit.ref, 'link': link.model_dump(), 'reason': 'unknown_or_reflexive_memory_link'})
                    continue
                grounded_links.append(link)
            edit.links = grounded_links
            role_refs = {role.entity_ref for role in edit.roles}
            for assertion in edit.assertions:
                for ref, role in [(assertion.subject_ref, 'subject'), (assertion.object_ref, 'object')]:
                    if ref not in role_refs and ref in entity_refs:
                        edit.roles.append(EntityRole(entity_ref=ref, role=role))
                        role_refs.add(ref)
            if any(link.target_ref not in refs | history.keys() for link in edit.links):
                raise ValueError('memory_link_requires_supplied_endpoint')
            if edit.time_expression and edit.end_expression:
                text = '\n'.join(e.quote for e in edit.evidence if e.source_id == primary['id'])
                begin = resolve_time(edit.time_expression, text, primary['message_at'])
                end = resolve_time(edit.end_expression, text, primary['message_at'])
                if begin and end and parse_time(begin) >= parse_time(end):
                    raise ValueError('validity_interval_requires_increasing_dates')
        return plan

    def _stage(self, name, state, cache, save, validator, deadline):
        if name in cache:
            return (ProposalPlan if name == 'propose' else MaintenancePlan).model_validate(cache[name])
        method = getattr(self.engine.model, name)
        call_state = state
        previous = cache.get('last_failure')
        if previous and previous.get('reason') != 'provider_unavailable':
            call_state = {**state, 'previous_output_failure': {**previous, 'last_validation': cache.get('validation', [])[-1:]}}
        try:
            raw = method(call_state, deadline=deadline)
            cache.setdefault('outputs', {})[name] = raw
            save()
            result = validator(raw, state)
        except ProviderUnavailable:
            cache['last_failure'] = {'stage': name, 'reason': 'provider_unavailable'}
            save()
            raise
        except (ValidationError, ValueError, OutputInvalid) as error:
            reason = str(error).split('\n')[0][:180] if not isinstance(error, ValidationError) else 'output_schema_invalid:' + ','.join('.'.join(map(str, e['loc'])) + ':' + e['type'] for e in error.errors()[:3])
            details = getattr(error, 'details', {})
            cache.setdefault('validation', []).append({'stage': name, 'error': reason, 'details': details})
            if cache.get('repair_used'):
                cache['last_failure'] = {'stage': name, 'reason': reason, 'details': details, 'repair_budget_exhausted': True}
                save()
                raise ProviderUnavailable('add_output_requires_retry') from None
            cache['repair_used'] = True
            save()
            try:
                guidance = {
                    'every_candidate_requires_supported_edit_or_explicit_disposition': 'Process every missing_candidate_ids entry in repair_errors.details through an edit or an explicit discarded entry. For a request, unsupported inference or transient proposal that should not persist, supply its discarded reason. Select only valid_candidate_ids and remove unexpected_candidate_ids.',
                    'role_requires_declared_entity': 'Use the exact entities[].ref for role endpoints. Source names map to the declared entity; clock times and temporary descriptions are attributes.',
                    'assertion_requires_declared_endpoints': 'An assertion subject uses a declared entity ref. Its object can use a declared entity ref or a literal value copied from original evidence.',
                    'assertion_object_requires_grounded_entity_or_literal': 'Use a declared entity ref for a named relation object, or copy its literal value from supplied evidence. Preserve unsupported interpretations only as explicit uncertainty in memory text.',
                    'new_entity_name_requires_source_mention': 'For a new named person, place or organization copy the actual surface name from its quoted source, or select an existing supplied identity. Store timestamps and temporary state descriptions as memory attributes.',
                    'ongoing_relation_identity_changed': 'This target maintains a different ongoing relationship. For an independent relationship or state, set target_id=null. For the SAME relationship use the existing canonical predicate from history[].assertions; express negation through negated=true. Preserve the unrelated target unchanged.',
                    'combine_edits_of_same_memory_target': 'A historical memory is selected by several edits. Match its exact maintained claim or particular occurrence. Combine edits that revise that same claim into ONE edit. Give any independent claim its own edit with target_id=null; an ongoing state and the event establishing it have separate identities.',
                    'memory_target_requires_recalled_history': 'Select an exact history[].id for the same claim or occurrence, or null for a new claim. Entity IDs are separate from memory IDs.',
                    'entity_target_requires_recalled_identity': 'Entity target_id uses candidate_entities[].id or JSON null. Local ref names and memory IDs have separate roles.',
                    'evidence_requires_supplied_source': 'Select source_id from the supplied sources. Every edit cites the new user source; context remains attributed to its original speaker.'
                }.get(reason, 'Generate a complete corrected plan satisfying the identified field or source constraint.')
                raw = method({**state, 'repair_errors': [{'error': reason, 'guidance': guidance, 'details': details}]}, deadline=deadline)
                cache.setdefault('outputs', {})[name + '_repair'] = raw
                save()
                result = validator(raw, state)
            except (ValidationError, ValueError, OutputInvalid) as error:
                cache['last_failure'] = {'stage': name, 'reason': 'output_schema_invalid' if isinstance(error, ValidationError) else str(error).split('\n')[0][:180], 'details': {'issues': [{'loc': list(e['loc']), 'type': e['type'], 'message': e['msg']} for e in error.errors(include_input=False)[:8]]} if isinstance(error, ValidationError) else getattr(error, 'details', {})}
                save()
                raise ProviderUnavailable('add_repair_requires_retry') from None
        if name == 'maintain':
            cache['graph_dispositions'] = result._graph_dispositions
            cache['write_compositions'] = result._write_compositions
            raw_targets = {e.get('ref'): e.get('target_id') for e in raw.get('edits', [])}
            cache['target_normalizations'] = [{'edit_ref': e.ref, 'proposed_target': raw_targets.get(e.ref), 'accepted_target': e.target_id, 'reason': 'memory_identity'}
                for e in result.edits if raw_targets.get(e.ref) != e.target_id]
        if name == 'propose':
            kept = {item.id for item in result.items}
            cache['source_dispositions'] = [{'candidate_id': item['id'], 'reason': 'context_only_evidence'}
                for item in raw.get('items', []) if item.get('id') not in kept]
        cache[name] = result.model_dump()
        cache.pop('last_failure', None)
        save()
        return result

    def _recall(self, state, proposal, deadline):
        owner = state['owner']
        as_of = state['new_user_source']['message_at'] or state['new_user_source']['created_at']
        memories = {m.id: m for raw in self.store.memories(owner) if (m := visible_memory(raw, as_of)) is not None}
        mentions = [mention for item in proposal.items for mention in item.entities]
        entities = entity_candidates(self.store, owner, mentions, as_of)
        matched = {e['id'] for e in entities if not e['is_self']}
        documents = self.engine._visible_documents(owner, as_of)
        vectors = self.engine._embed([item.content for item in proposal.items], deadline) if self.engine.embedder and documents else [None] * len(proposal.items)
        identifiers = []
        retrieval_trace = []
        for item, vector in zip(proposal.items, vectors):
            local_ids = {m.id for m in memories.values() if matched.intersection(m.object_ids)}
            local = [d for d in documents if d.memory_id in local_ids]
            all_memory = [d for d in documents if d.memory_id in memories]
            # Entity-local entry points plus a small cross-entity channel cover aliases and missing graph links.
            local_hits = self.engine._hybrid(owner, item.content, local, vector, limit=6)
            global_hits = self.engine._hybrid(owner, item.content, all_memory, vector, limit=6)
            selected = list(dict.fromkeys(d.memory_id for d, _ in [*local_hits, *global_hits]))[:8]
            identifiers.extend(selected)
            retrieval_trace.append({'candidate_id': item.id, 'local_memories': len(local_ids), 'selected': selected})
        identifiers = list(dict.fromkeys(identifiers))[:24]
        cards = []
        for key in identifiers:
            memory = memories[key]
            revision = memory.current
            source_cards = [{'id': s.id, 'role': s.role, 'content': s.content, 'message_at': s.message_at,
                             'created_at': s.created_at} for s in self.store.sources(owner, revision.source_ids)]
            cards.append({'id': key, 'revision_id': revision.id, 'tags': revision.tags,
                          'content': revision.content, 'status': revision.status, 'roles': [x.model_dump() for x in revision.roles],
                          'assertions': [x.model_dump() for x in revision.assertions], 'attributes': [x.model_dump() for x in revision.attributes], 'links': [x.model_dump() for x in revision.links],
                          'event_at': revision.event_at, 'valid_from': revision.valid_from, 'valid_to': revision.valid_to,
                          'temporal_scope': revision.temporal_scope, 'sources': source_cards})
        entity_ids = {role['entity_ref'] for card in cards for role in card['roles']}
        present = {e['id'] for e in entities}
        for raw in self.store.list('objects', owner):
            if raw['id'] in entity_ids - present and parse_time(raw['observed_at']) <= parse_time(as_of):
                obj = GraphObject.model_validate(raw)
                entities.append({**obj.model_dump(), 'aliases': [a.name for a in obj.aliases if parse_time(a.observed_at) <= parse_time(as_of)]})
                present.add(obj.id)
        query = ' '.join(item.content for item in proposal.items)
        raw_hits = self.engine._hybrid(owner, query, [d for d in documents if d.memory_id is None], vectors[0] if len(vectors) == 1 else None, limit=4)
        raw_ids = list(dict.fromkeys(sid for d, _ in raw_hits for sid in d.source_ids))[:8]
        raw_history = [{'id': s.id, 'role': s.role, 'content': s.content, 'message_at': s.message_at, 'created_at': s.created_at}
                       for s in self.store.sources(owner, raw_ids)]
        context = {**state, 'proposal': proposal.model_dump(), 'history': cards,
                   'candidate_entities': entities[:40], 'raw_history': raw_history, 'recall_trace': retrieval_trace}
        # Bound the semantic input while retaining complete referenced quote spans for validation.
        while len(json.dumps(context, ensure_ascii=False).encode()) > 64000 and context['raw_history']:
            context['raw_history'].pop()
        while len(json.dumps(context, ensure_ascii=False).encode()) > 64000 and context['history']:
            context['history'].pop()
        return context

    def _materialize(self, state, plan):
        owner = state['owner']
        primary = state['new_user_source']
        observed = primary['message_at'] or primary['created_at']
        sources = {x['id']: x for x in [primary, *state['context_sources'], *state['raw_history']]}
        sources.update({s['id']: s for card in state['history'] for s in card['sources']})
        entities, mapping = {}, {}
        used = {r.entity_ref for edit in plan.edits if edit.operation != 'keep' for r in edit.roles}
        for item in plan.entities:
            if item.ref not in used:
                continue
            key = item.target_id or (stable_id('obj_', owner, 'self') if item.is_self else stable_id('obj_', owner, primary['id'], item.ref, item.name))
            previous = self.store.get('objects', owner, key)
            obj = entities.get(key) or (GraphObject.model_validate(previous) if previous else GraphObject(id=key, owner=owner,
                name='self' if item.is_self else item.name, kind='person' if item.is_self else item.kind.casefold(),
                is_self=item.is_self, observed_at=observed))
            known = {(a.name, a.source_id) for a in obj.aliases}
            for name in dict.fromkeys([item.name, *item.aliases]):
                ev = next((e for e in item.evidence if normalized_name(name) in normalized_name(e.quote)), None)
                if ev and (name, ev.source_id) not in known:
                    source = sources[ev.source_id]
                    obj.aliases.append(AliasEvidence(name=name, source_id=ev.source_id, observed_at=source['message_at'] or source['created_at']))
            mapping[item.ref] = key
            entities[key] = obj
        ids = {edit.ref: edit.target_id or stable_id('mem_', owner, primary['id'], state.get('source_window', {}), edit.ref, edit.content) for edit in plan.edits}
        old_revisions = {h['id']: h['revision_id'] for h in state['history']}
        revision_ids = {e.ref: old_revisions[e.target_id] if e.operation == 'keep' else stable_id('rev_', owner, ids[e.ref], e.model_dump()) for e in plan.edits}
        memories, edges, expected = [], [], {}
        for edit in plan.edits:
            key = ids[edit.ref]
            operation = ('update' if edit.target_id else 'create') if edit.operation == 'write' else edit.operation
            raw = self.store.get('memories', owner, key)
            memory = Memory.model_validate(raw) if raw else None
            old = next((card for card in state['history'] if card['id'] == key), None)
            expected[key] = old['revision_id'] if old else None
            if edit.operation == 'keep':
                continue
            previous = visible_memory(memory, observed).current if memory and visible_memory(memory, observed) else None
            evidence = sorted(edit.evidence, key=lambda e: e.source_id != primary['id'])
            quote = '\n'.join(e.quote for e in evidence if e.source_id == primary['id'])
            begin = resolve_time(edit.time_expression, quote, primary['message_at'])
            end = resolve_time(edit.end_expression, quote, primary['message_at'])
            event_at = begin if 'event' in edit.tags else None
            if previous and 'event' in edit.tags and not edit.time_expression and edit.status == previous.status:
                event_at = previous.event_at
            valid_from = (begin or observed) if edit.temporal_scope != 'episodic' or 'event' not in edit.tags else None
            if previous and edit.change in {'confirm', 'supplement'}:
                valid_from = begin or previous.valid_from
                end = end or previous.valid_to
            if valid_from and end and parse_time(valid_from) >= parse_time(end):
                raise ValueError('validity_interval_inverted')
            roles = [r.model_copy(update={'entity_ref': mapping[r.entity_ref]}) for r in edit.roles]
            assertions = [a.model_copy(update={'subject_ref': mapping[a.subject_ref], 'object_ref': mapping[a.object_ref]}) for a in edit.assertions if a.object_ref in mapping]
            attributes = [LiteralAttribute(subject_ref=mapping[a.subject_ref], predicate=a.predicate, value=a.object_ref.removeprefix('literal:'), negated=a.negated) for a in edit.assertions if a.object_ref not in mapping]
            links = [BoundMemoryLink(target_ref=ids.get(link.target_ref, link.target_ref), label=link.label,
                target_revision_id=revision_ids[link.target_ref] if link.target_ref in revision_ids else old_revisions[link.target_ref]) for link in edit.links]
            if any(link.target_ref == key for link in links):
                raise ValueError('memory_link_requires_distinct_identity')
            revision = Revision(id=stable_id('rev_', owner, key, edit.model_dump()),
                content=absolute_content(edit.content, quote, primary['message_at']), source_ids=list(dict.fromkeys(e.source_id for e in evidence)),
                quote=evidence[0].quote, event_at=event_at, valid_from=valid_from, valid_to=end,
                observed_at=observed, created_at=utcnow(), status='retracted' if operation == 'retract' else edit.status,
                time_resolution='rule' if begin or valid_from else 'unknown', time_precision='day' if begin else 'unknown',
                operation=operation, tags=list(dict.fromkeys(edit.tags)), evidence=evidence, roles=roles,
                assertions=assertions, attributes=attributes, links=links, temporal_scope=edit.temporal_scope,
                replaces_revision_id=previous.id if previous else None, change='new' if operation == 'create' else 'withdraw' if operation == 'retract' else edit.change)
            if memory:
                memory.revisions.append(revision)
                memory.impact = edit.impact
            else:
                memory = Memory(id=key, owner=owner, revisions=[revision], impact=edit.impact)
            memories.append(memory)
            edges.extend(project_edges(memory, revision, previous.id if previous else None))
        return list(entities.values()), memories, edges, expected

    def add(self, request):
        from .engine import DuplicateRequestConflict
        import hashlib
        deadline = monotonic() + self.engine.settings.add_timeout
        with self.engine._scope(request.user_id, deadline):
            fingerprint = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            progress = self.store.get('requests', request.user_id, request.request_id)
            if progress and progress['fingerprint'] != fingerprint:
                raise DuplicateRequestConflict('request_id_payload_conflict')
            if progress and progress.get('pipeline') != PIPELINE:
                raise ProviderUnavailable('request_pipeline_requires_frozen_runtime_or_source_replay')
            if progress and progress['status'] == 'complete':
                return AddResponse(request_id=request.request_id, user_id=request.user_id, session_id=request.session_id)
            if not progress:
                from .models import Source
                from .clock import message_time
                created = utcnow()
                sources = [Source(id=stable_id('src_', request.user_id, request.request_id, i), owner=request.user_id,
                    session_id=request.session_id, request_id=request.request_id, ordinal=i, role=m.role, content=m.content,
                    message_at=message_time(m.timestamp), created_at=created) for i, m in enumerate(request.messages)]
                progress = {'fingerprint': fingerprint, 'pipeline': PIPELINE, 'status': 'pending',
                            'source_ids': [s.id for s in sources], 'units': {}, 'created_at': created}
                with self.store.transaction():
                    for source in sources:
                        self.store.put('sources', source.owner, source.id, source.model_dump())
                    self.store.put('requests', request.user_id, request.request_id, progress)
            def save():
                self.store.put('requests', request.user_id, request.request_id, progress)
            sources = self.store.sources(request.user_id, progress['source_ids'])
            for number, unit in enumerate(self.engine._units(sources)):
                user = next((s for s in unit if s.role == 'user'), None)
                blocks = list(self.chunks(user.content)) if user else [(0, 0, '')]
                for part, (start, stop, text) in enumerate(blocks):
                    key = str(number) if len(blocks) == 1 else f'{number}.{part}'
                    cache = progress['units'].setdefault(key, {'phase': 'pending', 'source_start': start, 'source_stop': stop})
                    if cache['phase'] == 'indexed':
                        continue
                    if monotonic() >= deadline:
                        raise ProviderUnavailable('add_deadline')
                    state = self.engine._add_state(user, unit, sources, deadline) if user and self.engine.model else None
                    if state:
                        state['new_user_source']['content'] = text
                        state['source_window'] = {'start': start, 'stop': stop}
                        proposal = self._stage('propose', state, cache, save, self._proposals, deadline)
                        if proposal.items:
                            if 'maintenance_context' not in cache:
                                cache['maintenance_context'] = self._recall(state, proposal, deadline)
                                save()
                            context = cache['maintenance_context']
                            plan = self._stage('maintain', context, cache, save, self._validated_plan, deadline)
                            entities, memories, edges, expected = self._materialize(context, plan)
                        else:
                            entities, memories, edges, expected = [], [], [], {}
                    else:
                        entities, memories, edges, expected = [], [], [], {}
                    documents = self.engine._documents(unit if part == 0 else [], memories)
                    pending = []
                    for document in documents:
                        previous = self.store.get('search_documents', request.user_id, document.id)
                        if previous and previous['text'] == document.text and (previous.get('vector') is not None or not self.engine.embedder):
                            continue
                        pending.append(document)
                    if pending and self.engine.embedder:
                        vectors = self.engine._embed([d.text for d in pending], deadline)
                        for doc, vector in zip(pending, vectors):
                            doc.vector = vector
                    try:
                        with self.store.transaction():
                            for target, expected_revision in expected.items():
                                stored = self.store.get('memories', request.user_id, target)
                                current = visible_memory(Memory.model_validate(stored), state['new_user_source']['message_at'] or state['new_user_source']['created_at']) if stored else None
                                if (current.current.id if current else None) != expected_revision:
                                    cache.pop('maintenance_context', None)
                                    cache.pop('maintain', None)
                                    raise ProviderUnavailable('maintenance_history_changed_retry')
                            for obj in entities:
                                self.store.put('objects', obj.owner, obj.id, obj.model_dump())
                            for memory in memories:
                                self.store.put('memories', memory.owner, memory.id, memory.model_dump())
                            for edge in edges:
                                self.store.put('edges', edge.owner, edge.id, edge.model_dump())
                            for document in pending:
                                self.store.put('search_documents', document.owner, document.id, document.model_dump())
                            cache.update(phase='indexed', memory_ids=[m.id for m in memories])
                            save()
                    except ProviderUnavailable:
                        save()
                        raise
            progress.update(status='complete', completed_at=utcnow())
            save()
        return AddResponse(request_id=request.request_id, user_id=request.user_id, session_id=request.session_id)
