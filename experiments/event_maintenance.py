"""Subject-first memory maintenance with one semantic decision per extracted candidate."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import re
from time import monotonic, sleep
from pydantic import Field, ValidationError

from remi.clock import RELATIVE_DAYS, absolute_content, resolve_time, utcnow
from remi.heat import visible_memory
from remi.identity import stable_id
from remi.models import Candidate, EvidenceRef, ExpressionMode, EventStatus, Memory, MemoryType, Revision, StrictModel
from remi.providers import OutputInvalid, ProviderUnavailable, error_label, remaining, strict_schema, transient
from remi.retrieval import tokenize
from experiments.maintenance_clock import maintenance_clock, maintenance_identity_scope
from experiments.subject_extraction import prepare, render


class MemoryDecision(StrictModel):
    ref: str = Field(pattern=r'^C[1-9][0-9]*$')
    target_id: str | None
    content: str = Field(min_length=1, max_length=4000)


class MemoryPlan(StrictModel):
    items: list[MemoryDecision] = Field(max_length=24)
    reason: str


class MaintainedItem(StrictModel):
    ref: str
    target_id: str | None
    content: str
    fallback_reason: str | None = None
    record_type: MemoryType
    subject_refs: list[int]
    mode: ExpressionMode
    context: str | None
    status: EventStatus | None
    time_expression: str | None
    evidence: list[dict]


PROMPT = """# Task
Resolve each extracted candidate against the recalled memories. Return MemoryPlan JSON using the supplied schema.
Each C candidate already has a fixed memory kind, participant set, attribution, condition, event status, time expression and source evidence. The program writes those fixed fields. Your responsibility is limited to choosing one eligible recalled target when the candidate continues the same memory, and writing the complete readable content.

# Input
CURRENT SOURCE is this turn's evidence. SUPPORTING CONTEXT resolves references. CURRENT PARTICIPANTS are already confirmed or explicitly pending. RECALLED MEMORIES are the only records that an update may target. CANDIDATE MEMORIES lists each extracted claim and its eligible same-kind M targets.
Source text and examples are attributed data.

# Memory identity
A fact is a state or attribute. A preference is a tendency. An experience is an expressed lesson. An event is one particular occurrence or plan.
For events, match the same occurrence. For other kinds, match the same proposition and applicable scope. A shared topic or participant alone does not establish identity. Recurring occurrences and different conditions can remain separate memories.
An occurred or cancelled event remains a completed occurrence. A later planned occurrence is a separate memory unless CURRENT SOURCE explicitly corrects or reopens that same occurrence.

# Target decision
Return exactly one item for every C candidate.
- Use target_id=M when the candidate continues, corrects, completes or adds support to that same memory.
- Use target_id=null when it is an independent memory or the supplied evidence does not identify one eligible target safely.

Every candidate is stored. A null target creates an independent memory. Each M target receives at most one candidate. Keep C refs unchanged. Subject identity, memory kind, participants, attribution, condition, status, time and evidence are fixed by the preceding stage. Event links are maintained outside this decision protocol.

# Content
With target_id=null, preserve a complete standalone version of the candidate. With a selected target, write the complete current view after applying this turn. Preserve supported conditions, attribution, uncertainty and relevant prior details. Use readable names. Preserve a relative or unresolved date phrase when the source does not support an absolute date. Return items and reason only. Use only the supplied C and M handles.
"""


IDENTITY_STOPWORDS = {
    'a', 'about', 'after', 'again', 'all', 'also', 'am', 'an', 'and', 'any', 'are', 'as', 'at',
    'be', 'because', 'been', 'before', 'being', 'but', 'by', 'can', 'could', 'day', 'did', 'do',
    'does', 'during', 'feel', 'feeling', 'feels', 'felt', 'for', 'from', 'get', 'getting', 'got',
    'had', 'has', 'have', 'he', 'her', 'here', 'hers', 'him', 'his', 'honestly', 'how', 'i', 'if',
    'in', 'into', 'is', 'it', 'its', 'just', 'know', 'later', 'like', 'little', 'made', 'make',
    'makes', 'maybe', 'me', 'mine', 'moment', 'moments', 'more', 'morning', 'most', 'much', 'my',
    'need', 'needed', 'needs', 'new', 'night', 'not', 'now', 'of', 'on', 'one', 'or', 'our',
    'ours', 'out', 'over', 'plan', 'planned', 'planning', 'plans', 'really', 'right', 'said', 'say',
    'scheduled', 'schedules', 'scheduling', 'she', 'should', 'some', 'something',
    'start', 'started', 'starting', 'still', 'tell', 'than', 'that', 'the', 'their', 'them', 'then',
    'there', 'these', 'they', 'thing', 'things', 'think', 'this', 'those', 'thought', 'to', 'today',
    'together', 'told', 'tonight', 'up', 'us', 'very', 'want', 'wanted', 'wants', 'was', 'way',
    'we', 'were', 'while', 'will', 'with', 'would', 'you', 'your', 'yours',
    'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september',
    'october', 'november', 'december',
}


def _identity_terms(text, labels=()):
    """Keep concrete topic terms while excluding participants and narrative glue."""
    excluded = set(IDENTITY_STOPWORDS)
    for label in labels:
        excluded.update(tokenize(label or ''))
    return {
        term for term in tokenize(text)
        if term not in excluded and not term.isdigit()
        and (not re.fullmatch(r'[a-z_]+', term) or len(term) >= 3)
    }


def eligible_targets(state):
    """Return a small candidate-specific target set based on concrete topic continuity."""
    labels = []
    for subject in state.get('subject_catalog', []):
        labels.append(subject.get('name'))
        labels.extend(alias.get('name') for alias in subject.get('aliases', []))
    result = {}
    for candidate in state.get('memory_candidates', []):
        candidate_terms = _identity_terms(candidate['content'], labels)
        scored = []
        for memory in state.get('history', []):
            if memory['record_type'] != candidate['record_type']:
                continue
            memory_terms = _identity_terms(memory['content'], labels)
            overlap = candidate_terms & memory_terms
            smaller = min(len(candidate_terms), len(memory_terms))
            coverage = len(overlap) / max(1, smaller)
            if len(overlap) >= 2 or len(overlap) == 1 and smaller <= 2:
                scored.append((memory['id'], len(overlap), coverage))
        scored.sort(key=lambda row: (-row[1], -row[2], row[0]))
        result[candidate['ref']] = [row[0] for row in scored[:3]]
    return result


def render_event(packet):
    packet = deepcopy(packet)
    if not packet['new_user_source'].get('message_at'):
        packet['new_user_source'].pop('created_at', None)
    body = render(packet)
    body = body.replace(
        'Return the complete items and reason JSON for CURRENT SOURCE using its current P handles.',
        'Return MemoryPlan JSON with one target decision for every C candidate.'
    )
    rows = ['# CANDIDATE MEMORIES']
    for item in packet.get('memory_candidates', []):
        participants = ', '.join(item['participant_handles']) or '(none)'
        targets = ', '.join(item['eligible_targets']) or '(none)'
        rows.extend([
            f"{item['ref']} | kind: {item['record_type']} | participants: {participants}",
            f"  candidate: {item['content']}",
            f"  mode: {item['mode']} | context: {item['context']} | status: {item['status']} | time expression: {item['time_expression']}",
            f"  eligible same-kind targets: {targets}",
        ])
    body += '\n\n' + '\n'.join(rows)
    if packet.get('recovery'):
        body += '\n# PENDING CLAIM REVIEW\nResolve exactly this prior claim: ' + packet['recovery']['content']
    return body


def readable_time(content, source, anchor):
    qualifiers = {
        'this morning': 'morning', 'this afternoon': 'afternoon',
        'this evening': 'evening', 'tonight': 'night', 'last night': 'night',
    }
    pattern = r'\b(?:' + '|'.join(qualifiers) + r')\b'
    def replace(match):
        stamp = resolve_time(match[0], source, anchor)
        return stamp[:10] + ' ' + qualifiers[match[0].casefold()] if stamp else match[0]
    return absolute_content(re.sub(pattern, replace, content, flags=re.IGNORECASE), source, anchor)


def memory_fingerprint(record):
    normalized = Memory.model_validate(record).model_dump()
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _wire_candidates(state, context):
    by_candidate = eligible_targets(state)
    return [dict(
        ref=row['ref'], content=row['content'], record_type=row['record_type'],
        participant_handles=[context['ref_handles'].get(ref, '(unavailable participant)') for ref in row['subject_refs']],
        mode=row['mode'], context=row.get('context'), status=row.get('status'),
        time_expression=row.get('time_expression'), eligible_targets=[
            context['memory_aliases'][target_id] for target_id in by_candidate.get(row['ref'], [])
        ],
    ) for row in state.get('memory_candidates', [])]


def provider_plan(model, state, *, deadline):
    packet, context = prepare(state)
    packet.update({key: deepcopy(state[key]) for key in ('recovery',) if key in state})
    packet['memory_candidates'] = _wire_candidates(state, context)
    schema = strict_schema(MemoryPlan)
    properties = schema['$defs']['MemoryDecision']['properties']
    candidate_refs = [row['ref'] for row in packet['memory_candidates']]
    if candidate_refs:
        properties['ref']['enum'] = candidate_refs
    memory_handles = list(context['memory_aliases'].values())
    properties['target_id'] = ({'anyOf': [{'type': 'string', 'enum': memory_handles}, {'type': 'null'}]}
                               if memory_handles else {'type': 'null'})
    messages = [dict(role='system', content=PROMPT)]
    for example, answer in examples():
        example_packet, example_context = prepare(example)
        example_packet['memory_candidates'] = _wire_candidates(example, example_context)
        messages.extend([
            dict(role='user', content='# INDEPENDENT EXAMPLE\n' + render_event(example_packet)),
            dict(role='assistant', content=json.dumps(answer, ensure_ascii=False)),
        ])
    messages.append(dict(role='user', content='# MEMORY MAINTENANCE INPUT\n' + render_event(packet)))
    request = dict(
        model=model.model, temperature=0, max_completion_tokens=3000, messages=messages,
        response_format=dict(type='json_schema', json_schema=dict(name='MemoryPlan', strict=True, schema=schema)),
    )
    for attempt in range(2):
        started = monotonic()
        trace = dict(kind='add_propose', model=model.model, attempt=attempt + 1,
                     pending_recovery=bool(state.get('recovery')))
        try:
            response = model.client.chat.completions.create(**request, timeout=remaining(deadline, model.timeout))
            if response.usage:
                trace.update(input_tokens=response.usage.prompt_tokens, output_tokens=response.usage.completion_tokens)
            message = response.choices[0].message
            if not message.content or getattr(message, 'refusal', None):
                raise OutputInvalid('memory_response_unavailable')
            result = json.loads(message.content)
            if not isinstance(result, dict):
                raise OutputInvalid('memory_plan_requires_object')
            reverse = {handle: original for original, handle in context['memory_aliases'].items()}
            for row in result.get('items', []) if isinstance(result.get('items'), list) else []:
                if isinstance(row, dict) and isinstance(row.get('target_id'), str):
                    row['target_id'] = reverse.get(row['target_id'], row['target_id'])
            return result
        except (ValueError, OutputInvalid) as error:
            trace.update(error_label(error))
            raise OutputInvalid('memory_response_invalid') from error
        except Exception as error:
            trace.update(error_label(error))
            if attempt == 0 and transient(error) and deadline - monotonic() > 1:
                sleep(.2)
                continue
            raise ProviderUnavailable('memory_provider_unavailable') from error
        finally:
            trace['seconds'] = monotonic() - started
            model.calls.append(trace)
    raise ProviderUnavailable('memory_provider_unavailable')


def examples():
    def state(text, candidate, history=()):
        source = dict(id='now', role='user', content=text, message_at='2029-06-01T10:00:00+00:00')
        return dict(new_user_source=source, context_sources=[], resolved_subjects=[dict(
            ref=0, id='person', name='Mira', kind='person', status='resolved', aliases=[])],
            history=list(history), subject_catalog=[dict(id='person', name='Mira')],
            memory_candidates=[dict(ref='C1', subject_refs=[0], mode='asserted',
                evidence=[dict(source_id='now')], **candidate)])
    old_event = dict(id='old', record_type='event', content='Mira plans a studio visit on June 3.',
        entity_ids=['person'], status='planned', planned_at='2029-06-03T00:00:00+00:00',
        sources=[dict(id='before', role='user', content='Mira plans a studio visit on June 3.',
                     message_at='2029-05-31T10:00:00+00:00')])
    old_preference = dict(id='pref', record_type='preference', content='Mira prefers written agendas at work.',
        entity_ids=['person'], status='stated', planned_at=None,
        sources=[dict(id='pref-before', role='user', content='Mira prefers written agendas at work.',
                     message_at='2029-05-31T10:00:00+00:00')])
    completed_event = dict(id='completed', record_type='event', content='Mira completed a studio visit on May 28.',
        entity_ids=['person'], status='occurred', planned_at='2029-05-28T00:00:00+00:00', event_at='2029-05-28T00:00:00+00:00',
        sources=[dict(id='completed-before', role='user', content='Mira completed a studio visit on May 28.',
                     message_at='2029-05-28T18:00:00+00:00')])
    cases = [
        (state('Mira plans another studio visit on June 8.', dict(content='Mira plans another studio visit on June 8.',
            record_type='event', status='planned', time_expression='June 8'), [old_event]),
         dict(target_id=None, content='Mira plans another studio visit on June 8.')),
        (state('Mira moved that studio visit to June 4.', dict(content='Mira moved the studio visit to June 4.',
            record_type='event', status='planned', time_expression='June 4'), [old_event]),
         dict(target_id='M1', content='Mira plans the studio visit on June 4.')),
        (state('Mira still prefers written agendas at work.', dict(content='Mira still prefers written agendas at work.',
            record_type='preference', status=None, time_expression=None, context='at work'), [old_preference]),
         dict(target_id='M1', content='Mira prefers written agendas at work.')),
        (state('Mira booked another studio visit for June 8.', dict(content='Mira booked another studio visit for June 8.',
            record_type='event', status='planned', time_expression='June 8'), [completed_event]),
         dict(target_id=None, content='Mira booked another studio visit for June 8.')),
        (state('Mira changed that appointment, but the recalled visits do not identify which one.', dict(
            content='Mira changed an appointment.', record_type='event', status='planned', time_expression=None), [old_event]),
         dict(target_id=None, content='Mira changed an appointment.')),
    ]
    for packet, decision in cases:
        answer = dict(items=[dict(ref='C1', **decision)], reason='The operation follows the occurrence or proposition identity in the supplied evidence.')
        yield packet, MemoryPlan.model_validate(answer).model_dump()


def validate_plan(raw, state, view, *, protected=None):
    """Validate target selections and fall back to independent Add for every invalid slot."""
    del view, protected
    accepted, issues, fallback_reasons = {}, [], {}
    rows = raw.get('items') if isinstance(raw, dict) else None
    if not isinstance(rows, list) or len(rows) > 24:
        issues.append(dict(path=['items'], code='memory_plan_requires_at_most_24_items'))
        rows = []
    candidate_rows = {row['ref']: row for row in state.get('memory_candidates', [])}
    history = {memory['id']: memory for memory in state.get('history', [])}
    candidate_targets = eligible_targets(state)
    declared = Counter(row.get('ref') for row in rows if isinstance(row, dict) and isinstance(row.get('ref'), str))
    selected_targets = Counter(row.get('target_id') for row in rows if isinstance(row, dict)
                               and isinstance(row.get('target_id'), str))
    accepted_refs = set()
    for index, row in enumerate(rows):
        row_issues = []
        def fail(field, code, **details):
            issue = dict(path=['items', index, field], code=code, **details)
            row_issues.append(issue)
            issues.append(issue)
        try:
            decision = MemoryDecision.model_validate(row)
        except ValidationError as error:
            validation_issues = [dict(path=['items', index, *entry['loc']], code=entry['type']) for entry in error.errors()]
            issues.extend(validation_issues)
            if isinstance(row, dict) and row.get('ref') in candidate_rows:
                fallback_reasons.setdefault(row['ref'], validation_issues[0]['code'])
            continue
        candidate = candidate_rows.get(decision.ref)
        if candidate is None:
            fail('ref', 'select_supplied_candidate_ref')
        elif declared[decision.ref] != 1:
            fail('ref', 'return_each_candidate_once')
        target = history.get(decision.target_id) if decision.target_id else None
        if decision.target_id is not None:
            if candidate is not None and (target is None or target['record_type'] != candidate['record_type']):
                fail('target_id', 'target_requires_recalled_same_kind_memory')
            elif candidate is not None and decision.target_id not in candidate_targets.get(decision.ref, []):
                fail('target_id', 'target_requires_candidate_topic_continuity')
            elif candidate is not None and candidate['record_type'] == 'event':
                previous_status = target.get('status')
                current_status = candidate.get('status')
                if previous_status in {'occurred', 'cancelled'} and current_status in {'planned', 'desired'}:
                    fail('target_id', 'terminal_event_cannot_return_to_planned')
                elif previous_status in {'occurred', 'cancelled'} and current_status == 'occurred':
                    expression = candidate.get('time_expression') or _single_source_time_expression(state['new_user_source'])
                    current_time = resolve_time(expression, state['new_user_source']['content'],
                                                state['new_user_source'].get('message_at')) if expression else None
                    previous_time = target.get('event_at')
                    if current_time and previous_time and current_time[:10] != previous_time[:10]:
                        fail('target_id', 'terminal_event_date_identifies_separate_occurrence')
            if selected_targets[decision.target_id] != 1:
                fail('target_id', 'one_candidate_per_target')
        relevant = [state['new_user_source'], *(target.get('sources', []) if target else [])]
        for date in re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', decision.content):
            if not any(resolve_time(date, source['content'], source.get('message_at')) for source in relevant):
                fail('content', 'readable_date_requires_source_support', actual=date)
        if candidate is not None and not row_issues:
            accepted[str(index)] = {**deepcopy(candidate), 'content': decision.content,
                'target_id': decision.target_id, 'fallback_reason': None}
            accepted_refs.add(decision.ref)
        elif candidate is not None and row_issues:
            fallback_reasons.setdefault(decision.ref, row_issues[0]['code'])
    for position, candidate in enumerate(state.get('memory_candidates', []), start=len(rows)):
        if candidate['ref'] in accepted_refs:
            continue
        reason = 'candidate_omitted_by_model' if declared[candidate['ref']] == 0 else (
            fallback_reasons.get(candidate['ref'], 'candidate_decision_invalid'))
        accepted[f'fallback:{position}'] = {**deepcopy(candidate), 'content': candidate['content'],
            'target_id': None, 'fallback_reason': reason}
    return accepted, issues


def candidates(accepted, state):
    result = []
    for row in accepted.values():
        citations = sorted(row['evidence'], key=lambda evidence: evidence['source_id'] != state['new_user_source']['id'])
        result.append(Candidate(id=row['ref'], connection=None, content=row['content'], subject_refs=row['subject_refs'],
            record_type=row['record_type'], mode=row['mode'], context=row.get('context'), status=row.get('status'),
            time_expression=row.get('time_expression'), evidence=[EvidenceRef(
                source_id=evidence['source_id'], quote=state['source_catalog'][evidence['source_id']]['content'])
                for evidence in citations]).model_dump())
    return result


def _single_source_time_expression(primary):
    """Return one unambiguous, source-grounded date phrase for deterministic extraction fallback."""
    text=primary['content'];patterns=[
        r'\d{4}-\d{2}-\d{2}(?:T[0-9:.+Z-]+)?',
        r'(?:(?:\d{4})年)?\d{1,2}月\d{1,2}日',
        r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b',
        r'\b\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?(?:,?\s+\d{4})?\b',
        r'\b(?:'+ '|'.join(re.escape(value) for value in sorted((key for key in RELATIVE_DAYS if key.isascii()),key=len,reverse=True))+r')\b|今天|昨天|明天',
    ]
    matches={}
    for pattern in patterns:
        for match in re.finditer(pattern,text,re.IGNORECASE):
            matches[match.span()]=match[0]
    grounded=[value for _,value in sorted(matches.items()) if resolve_time(value,text,primary.get('message_at'))]
    unique=list(dict.fromkeys(value.casefold() for value in grounded))
    return grounded[0] if len(unique)==1 else None


def _apply_candidate_time(values, item, primary):
    if item.record_type != 'event':
        return
    field = 'event_at' if item.status == 'occurred' else 'planned_at' if item.status in {'planned', 'desired'} else None
    if field is None:
        return
    expression = item.time_expression or _single_source_time_expression(primary)
    if not expression:
        return
    stamp = resolve_time(expression, primary['content'], primary.get('message_at'))
    precision = 'instant' if stamp and 'T' in stamp else 'day' if stamp else 'unknown'
    values[field] = stamp
    values['time_resolution'] = 'rule' if stamp else 'unknown'
    values['time_precision'] = precision
    details = [entry for entry in values.get('time_evidence', []) if entry['field'] != field]
    details.append(dict(field=field, expression=expression, source_id=primary['id'],
                        anchor=primary.get('message_at'), value=stamp, precision=precision))
    values['time_evidence'] = details


def materialize(store, state, bindings, accepted):
    owner = state['owner']
    primary = state['new_user_source']
    observed = maintenance_clock(state)
    user_id = stable_id('obj_', owner, 'user')
    history = {memory['id']: memory for memory in state.get('history', [])}
    memories, edges, expected, pending, dispositions = [], [], {}, [], []
    identity_scope = maintenance_identity_scope(state)
    items = {row['ref']: MaintainedItem.model_validate(row) for row in accepted.values()}
    candidate_rows = {row['id']: row for row in candidates(accepted, state)}
    for ref, item in items.items():
        candidate = Candidate.model_validate(candidate_rows[ref])
        current_ids = list(dict.fromkeys(bindings['subject_ids'][str(subject_ref)] for subject_ref in item.subject_refs
                                         if str(subject_ref) in bindings['subject_ids']))
        evidence = list(candidate.evidence)
        previous = None
        if item.target_id is not None:
            raw = store.get('memories', owner, item.target_id)
            if raw is None:
                raise ProviderUnavailable('event_history_changed_retry')
            snapshot = state.get('memory_snapshot_hashes', {}).get(item.target_id)
            if snapshot is not None and memory_fingerprint(raw) != snapshot:
                raise ProviderUnavailable('event_history_changed_retry')
            memory = Memory.model_validate(raw)
            historical = visible_memory(memory, observed)
            if historical is None or historical.current.id != history[item.target_id]['revision_id']:
                raise ProviderUnavailable('event_history_changed_retry')
            previous = historical.current
            expected[memory.id] = raw
            evidence = list({entry.source_id: entry for entry in [*candidate.evidence, *previous.evidence]}.values())
            values = previous.model_dump()
            mid = memory.id
            values.update(content=item.content, mode=item.mode,
                          context=item.context or previous.context,
                          status=item.status if item.record_type == 'event' and item.status is not None else previous.status,
                          entity_ids=list(dict.fromkeys([*previous.entity_ids, *current_ids])))
        else:
            mid = stable_id('mem_', owner, primary['id'], identity_scope, ref)
            expected[mid] = None
            memory = None
            values = dict(content=item.content, entity_ids=current_ids, mode=item.mode,
                context=item.context or 'source_reported',
                status=item.status if item.record_type == 'event' and item.status is not None else ('unknown' if item.record_type == 'event' else 'stated'),
                planned_at=None, event_at=None, time_resolution='unknown', time_precision='unknown', time_evidence=[])
        _apply_candidate_time(values, item, primary)
        values['content'] = readable_time(values['content'], primary['content'], primary.get('message_at'))
        if previous:
            fields = [('content', 'content'), ('entity_ids', 'participants'), ('mode', 'mode'), ('context', 'context'),
                      ('status', 'status'), ('planned_at', 'planned_at'), ('event_at', 'event_at')]
            changed = [label for field, label in fields if values[field] != getattr(previous, field)]
        else:
            changed = ['content', 'participants', 'mode', 'context', 'status', 'planned_at', 'event_at']
        support = bool(previous) and not changed
        if state.get('recovery'):
            clarification = state['context_sources'][-1]
            evidence.append(EvidenceRef(source_id=clarification['id'], quote=clarification['content']))
            evidence = list({entry.source_id: entry for entry in evidence}.values())
        operation = 'support' if support else 'update' if previous else 'create'
        values.update(id=stable_id('rev_', mid, primary['id'], identity_scope, candidate.id),
            source_ids=[entry.source_id for entry in evidence], evidence=[entry.model_dump() for entry in evidence],
            quote=candidate.evidence[0].quote, observed_at=observed, created_at=utcnow(), operation=operation,
            replaces_revision_id=previous.id if previous else None,
            user_involvement='involved' if user_id in values['entity_ids'] else 'not_involved')
        patch_evidence = evidence if state.get('recovery') else candidate.evidence
        values['maintenance_patch'] = dict(fields=changed,
            values={key: deepcopy(values[key]) for key in ('content', 'entity_ids', 'mode', 'context', 'status', 'planned_at', 'event_at', 'time_evidence')},
            participant_mode='add' if previous else 'replace', participant_ids=current_ids,
            evidence=[entry.model_dump() for entry in patch_evidence], evidence_mode=item.mode,
            related_add=[], related_remove=[], source_id=primary['id'], base_revision_id=previous.id if previous else None)
        revision = Revision.model_validate(values)
        if memory:
            memory.revisions.append(revision)
            memory = Memory.model_validate(memory.model_dump())
        else:
            memory = Memory(id=mid, owner=owner, record_type=item.record_type, revisions=[revision])
        memories.append(memory)
        disposition = ('supported' if support else 'updated') if previous else (
            'created_fallback' if item.fallback_reason else 'created')
        dispositions.append(dict(candidate_id=candidate.id, status='accepted', operation=disposition,
            memory_id=mid, reason=item.fallback_reason or 'source_validated_memory_maintenance'))
    from experiments.revision_replay import replay_revisions
    memories, edges = replay_revisions(store, memories, edges)
    return list(bindings['entity_rows']), memories, edges, expected, pending, dispositions
