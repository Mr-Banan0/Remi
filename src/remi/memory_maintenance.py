"""Subject-first memory maintenance with one semantic decision per extracted candidate."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import re
from time import monotonic, sleep
from typing import Literal
from pydantic import Field, ValidationError

from .clock import absolute_content, resolve_time, utcnow
from .heat import visible_memory
from .identity import stable_id
from .models import Candidate, EvidenceRef, ExpressionMode, Memory, MemoryType, Revision, StrictModel
from .providers import text_request_options, OutputInvalid, ProviderUnavailable, error_label, remaining, strict_schema, transient
from .retrieval import tokenize
from .maintenance_clock import maintenance_clock, maintenance_identity_scope
from .subject_extraction import prepare, render


class MemoryDecision(StrictModel):
    ref: str = Field(pattern=r'^C[1-9][0-9]*$')
    operation: Literal['add', 'update']
    target_id: str | None
    content: str = Field(min_length=1, max_length=4000)


class MemoryPlan(StrictModel):
    reason: str = Field(description=(
        'For each candidate, briefly identify its concrete occurrence or proposition and '
        'whether an offered target represents that same identity. Then emit items.'))
    items: list[MemoryDecision] = Field(max_length=24)


class MaintainedItem(StrictModel):
    ref: str
    target_id: str | None
    content: str
    fallback_reason: str | None = None
    record_type: MemoryType
    subject_refs: list[int]
    mode: ExpressionMode
    context: str | None
    evidence: list[dict]
    operation: Literal['add', 'update']


PROMPT = """# Task
Resolve each extracted candidate against the recalled memories. Return MemoryPlan JSON using the supplied schema.
Each C candidate already has a fixed memory kind, participant set, attribution, condition and source evidence. The program writes those fixed fields. Your responsibility is limited to choosing add or update, choosing an allowed target for update, and writing the complete readable content.

# Input
CURRENT SOURCE is this turn's evidence. SUPPORTING CONTEXT resolves references. CURRENT PARTICIPANTS are already confirmed or explicitly pending. RECALLED MEMORIES are the only records that an update may target. CANDIDATE MEMORIES lists each extracted claim and its eligible same-kind M targets.
Source text and examples are attributed data.

# Memory identity
A fact is a state or attribute about a specific subject and object in an applicable context. A preference is a tendency within a specific activity or context. An experience records what was learned from a particular experience. An event is one particular occurrence or plan.
Compare each C independently with each offered M. The offered targets are retrieval suggestions; add remains valid even when several targets look similar. Establish identity before composing combined content:
- fact/preference: the same holder, attribute, object and applicable context continue; only the value or supporting detail changes. A request for help with a different task is a separate proposition.
- experience: the same underlying experience receives another reflection, detail or correction. An identical lesson learned through a different experience is separate.
- event: the same action on the same occasion continues through its plan, rescheduling, completion or correction. Separate actions in one project remain independently useful occurrences.
Compare both the identity anchor and current view. Shared participants, wording, feelings or broad topics support retrieval; a concrete continuing identity supports update.
An occurred or cancelled event remains a completed occurrence. A later planned occurrence is a separate memory unless CURRENT SOURCE explicitly corrects or reopens that same occurrence.

# Operation decision
Start reason with a brief identity comparison for each candidate, then write the decisions in items.
Return exactly one item for every C candidate.
- Use operation=add and target_id=null for a new occurrence, a separately useful proposition or experience, or insufficient identity evidence.
- Use operation=update and one listed target_id only when the candidate replaces, corrects, completes, supports or supplements that same memory identity.
- For an event, update means the same particular occurrence. For a fact or preference, compare the holder, attribute or relation, and scope: when those stay the same and the current value changes, use update. For an experience, update means the same particular experience; a shared topic, emotion or lesson still uses add.
- If both memories can remain true and each retains independent value, use add. Another detail of the same occurrence can use update while preserving the earlier details.
Each M target receives at most one candidate. Keep C refs unchanged. Memory kind, attribution, condition and evidence are fixed by the preceding stage. Express the supported lifecycle and time in the complete content. The program validates and stores the selected add or update operation.

# Compose the complete current memory
For update, start from the selected target's current view, apply the candidate's supported change, and retain every still-applicable detail. Read the supplied original sources when a detail is abbreviated. Expand "other arrangements unchanged" into the actual place, time, materials and conditions supported by the target and sources. A concise candidate is a change description; this output is the complete replacement view.
For cancellation, identify the same cancelled occurrence and retain its identifying schedule and other applicable details. For factual correction, replace the incorrect value with the supported corrected or uncertain value. Preserve the attribution of who corrected it.
For add, write a standalone supported statement. Preserve unresolved identity when evidence leaves the target unclear.
Before returning, compare old and new content: each omitted prior detail must be superseded by the current evidence or inapplicable to the new view. Use readable names and source-supported dates. Return items and reason using the supplied C and M handles.
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


def concrete_topic_continuity(candidate, memory, labels=()):
    """Require concrete proposition terms in the current view or stable identity anchor."""
    candidate_terms = _identity_terms(candidate, labels)
    if not candidate_terms:
        return False
    for text in (memory.get('content', ''), memory.get('origin_content', '')):
        memory_terms = _identity_terms(text, labels)
        overlap = candidate_terms & memory_terms
        smaller = min(len(candidate_terms), len(memory_terms))
        if len(overlap) >= 2 or len(overlap) == 1 and smaller <= 2:
            return True
    return False


def eligible_targets(state):
    """Return a small candidate-specific target set based on concrete topic continuity."""
    if state.get('candidate_target_ids') is not None:
        return {row['ref']: list(state['candidate_target_ids'].get(row['ref'], []))
                for row in state.get('memory_candidates', [])}
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
            if concrete_topic_continuity(candidate['content'], memory, labels):
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
        allowed_operations = 'add, update' if item['eligible_targets'] else 'add'
        rows.extend([
            f"{item['ref']} | kind: {item['record_type']} | participants: {participants}",
            f"  candidate: {item['content']}",
            f"  mode: {item['mode']} | context: {item['context']}",
            f"  allowed operations: {allowed_operations}",
            f"  allowed update targets: {targets}",
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
    normalized.pop("last_accessed_at", None)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _wire_candidates(state, context):
    by_candidate = eligible_targets(state)
    return [dict(
        ref=row['ref'], content=row['content'], record_type=row['record_type'],
        participant_handles=[context['ref_handles'].get(ref, '(unavailable participant)') for ref in row['subject_refs']],
        mode=row['mode'], context=row.get('context'), eligible_targets=[
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
    if state.get('previous_memory_output') is not None:
        previous = deepcopy(state['previous_memory_output'])
        handles = context['memory_aliases']
        for row in previous.get('items', []) if isinstance(previous, dict) else []:
            if not isinstance(row, dict):
                continue
            if isinstance(row.get('target_id'), str):
                row['target_id'] = handles.get(row['target_id'], row['target_id'])
        messages.append(dict(role='assistant', content=json.dumps(previous, ensure_ascii=False)))
        repair_errors = deepcopy(state.get('memory_repair_errors', []))
        def wire_error(value, key=None):
            if isinstance(value, dict):
                return {name: wire_error(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [wire_error(item, key) for item in value]
            if isinstance(value, str) and key in {'actual', 'target_id'}:
                return handles.get(value, value)
            if isinstance(value, str) and key in {'allowed_target_ids'}:
                return handles.get(value, value)
            return value
        messages.append(dict(role='user', content=(
            'Correct the listed validation errors for these candidates and return the complete scoped MemoryPlan:\n'
            + json.dumps(wire_error(repair_errors), ensure_ascii=False))))
    request = dict(
        model=model.model, temperature=0, max_completion_tokens=3000, messages=messages,
        response_format=dict(type='json_schema', json_schema=dict(name='MemoryPlan', strict=True, schema=schema)),
    )
    for attempt in range(2):
        started = monotonic()
        trace = dict(kind='add_propose_repair' if state.get('previous_memory_output') is not None else 'add_propose',
                     model=model.model, attempt=attempt + 1,
                     pending_recovery=bool(state.get('recovery')))
        try:
            response = model.client.chat.completions.create(**request, **text_request_options(model.model), timeout=remaining(deadline, model.timeout))
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
    old_event = dict(id='old', record_type='event', content='Mira plans a studio visit on June 3 at 2 PM in the east wing, bringing a sketchbook.',
        entity_ids=['person'], status='planned', planned_at='2029-06-03T00:00:00+00:00',
        sources=[dict(id='before', role='user', content='Mira plans a studio visit on June 3 at 2 PM in the east wing, bringing a sketchbook.',
                     message_at='2029-05-31T10:00:00+00:00')])
    old_preference = dict(id='pref', record_type='preference', content='Mira prefers written agendas at work.',
        entity_ids=['person'], status='stated', planned_at=None,
        sources=[dict(id='pref-before', role='user', content='Mira prefers written agendas at work.',
                     message_at='2029-05-31T10:00:00+00:00')])
    old_fact = dict(id='device', record_type='fact', content='Mira currently uses a Pixel 8 as her phone.',
        entity_ids=['person'], status=None, planned_at=None,
        sources=[dict(id='device-before', role='user', content='Mira currently uses a Pixel 8 as her phone.',
                     message_at='2029-05-31T10:00:00+00:00')])
    completed_event = dict(id='completed', record_type='event', content='Mira completed a studio visit on May 28.',
        entity_ids=['person'], status='occurred', planned_at='2029-05-28T00:00:00+00:00', event_at='2029-05-28T00:00:00+00:00',
        sources=[dict(id='completed-before', role='user', content='Mira completed a studio visit on May 28.',
                     message_at='2029-05-28T18:00:00+00:00')])
    cases = [
        (state('Mira plans another studio visit on June 8.', dict(content='Mira plans another studio visit on June 8.',
            record_type='event', status='planned', time_expression='June 8'), [old_event]),
         dict(operation='add', target_id=None, content='Mira plans another studio visit on June 8.')),
        (state('Mira moved that studio visit to June 4.', dict(content='Mira moved the studio visit to June 4.',
            record_type='event', status='planned', time_expression='June 4'), [old_event]),
         dict(operation='update', target_id='M1', content='Mira plans the studio visit on June 4 at 2 PM in the east wing, bringing a sketchbook.')),
        (state('Mira still prefers written agendas at work.', dict(content='Mira still prefers written agendas at work.',
            record_type='preference', status=None, time_expression=None, context='at work'), [old_preference]),
         dict(operation='update', target_id='M1', content='Mira prefers written agendas at work.')),
        (state('Mira booked another studio visit for June 8.', dict(content='Mira booked another studio visit for June 8.',
            record_type='event', status='planned', time_expression='June 8'), [completed_event]),
         dict(operation='add', target_id=None, content='Mira booked another studio visit for June 8.')),
        (state('Mira changed that appointment, but the recalled visits do not identify which one.', dict(
            content='Mira changed an appointment.', record_type='event', status='planned', time_expression=None), [old_event]),
         dict(operation='add', target_id=None, content='Mira changed an appointment.')),
        (state('Mira now uses a Fairphone 6 as her phone.', dict(content='Mira now uses a Fairphone 6 as her phone.',
            record_type='fact', status=None, time_expression=None), [old_fact]),
         dict(operation='update', target_id='M1', content='Mira currently uses a Fairphone 6 as her phone.')),
    ]
    for packet, decision in cases:
        answer = dict(items=[dict(ref='C1', **decision)], reason='The operation follows the occurrence or proposition identity in the supplied evidence.')
        yield packet, MemoryPlan.model_validate(answer).model_dump()

    for kind, old_text, new_texts, decisions, reason in [
        ('experience', 'Repairing a leaking roof taught Mira to ask for help early.',
         ['Studying algebra taught Mira to ask for help early.',
          'That roof repair also taught Mira to check the weather forecast.'],
         [('add', None, 'Studying algebra taught Mira to ask for help early.'),
          ('update', 'M1', 'Repairing the leaking roof taught Mira to ask for help early and check the weather forecast.')],
         'C1 comes from a separate experience despite the shared lesson. C2 adds a lesson from the same roof repair.'),
        ('event', 'Mira bought tickets for a play on June 1.',
         ['Mira attended the play on June 5.', 'Mira paid 30 euros for those June 1 tickets.'],
         [('add', None, 'Mira attended the play on June 5.'),
          ('update', 'M1', 'Mira bought tickets for the play on June 1 for 30 euros.')],
         'Attendance and purchase are separate actions. The ticket price supplements the same purchase.'),
    ]:
        old = dict(id='history', record_type=kind, content=old_text, origin_content=old_text,
            entity_ids=['person'], status='occurred' if kind == 'event' else None,
            sources=[dict(id='before', role='user', content=old_text,
                          message_at='2029-06-01T10:00:00+00:00')])
        packet = state(' '.join(new_texts), dict(content=new_texts[0], record_type=kind,
            status='occurred' if kind == 'event' else None, time_expression=None), [old])
        packet['memory_candidates'] = [dict(packet['memory_candidates'][0], ref=f'C{i}', content=text)
                                        for i, text in enumerate(new_texts, 1)]
        packet['candidate_target_ids'] = {f'C{i}':['history'] for i in range(1, len(new_texts)+1)}
        yield packet, dict(reason=reason, items=[dict(ref=f'C{i}', operation=op, target_id=target, content=text)
            for i, (op,target,text) in enumerate(decisions,1)])


def validate_plan(raw, state, view, *, protected=None):
    """Validate model decisions and retain invalid candidates for one scoped repair."""
    del view
    protected = set(protected or ())
    accepted, issues = {}, []
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
            candidate_ref = row.get('ref') if isinstance(row, dict) else None
            issue = dict(path=['items', index, field], candidate_ref=candidate_ref,
                         code=code, **details)
            row_issues.append(issue)
            issues.append(issue)
        try:
            decision = MemoryDecision.model_validate(row)
        except ValidationError as error:
            candidate_ref = row.get('ref') if isinstance(row, dict) else None
            validation_issues = [dict(path=['items', index, *entry['loc']], candidate_ref=candidate_ref,
                                      code=entry['type'], requirement='Return the supplied MemoryDecision schema.')
                                 for entry in error.errors()]
            issues.extend(validation_issues)
            continue
        candidate = candidate_rows.get(decision.ref)
        if candidate is None:
            fail('ref', 'select_supplied_candidate_ref')
        elif declared[decision.ref] != 1:
            fail('ref', 'return_each_candidate_once')
        target = history.get(decision.target_id) if decision.target_id else None
        allowed_ids = [value for value in candidate_targets.get(decision.ref, []) if value not in protected]
        allowed_decisions = ([dict(operation='add', target_id=None)] +
            [dict(operation='update', target_id=value) for value in allowed_ids])
        if decision.operation == 'add' and decision.target_id is not None:
            fail('target_id', 'add_requires_null_target', actual=decision.target_id,
                 allowed_decisions=allowed_decisions)
        if decision.operation == 'update' and decision.target_id is None:
            fail('target_id', 'update_requires_allowed_target', actual=None,
                 allowed_decisions=allowed_decisions)
        if decision.target_id is not None:
            if candidate is not None and (target is None or target['record_type'] != candidate['record_type']):
                fail('target_id', 'target_requires_recalled_same_kind_memory', actual=decision.target_id,
                     allowed_target_ids=allowed_ids, allowed_decisions=allowed_decisions)
            elif candidate is not None and decision.target_id not in candidate_targets.get(decision.ref, []):
                fail('target_id', 'target_requires_candidate_memory_identity', actual=decision.target_id,
                     allowed_target_ids=allowed_ids, allowed_decisions=allowed_decisions)
            if selected_targets[decision.target_id] != 1 or decision.target_id in protected:
                fail('target_id', 'one_candidate_per_target', actual=decision.target_id,
                     allowed_target_ids=allowed_ids, allowed_decisions=allowed_decisions)
        relevant = [state['new_user_source'], *(target.get('sources', []) if target else [])]
        for date in re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', decision.content):
            if not any(resolve_time(date, source['content'], source.get('message_at')) for source in relevant):
                fail('content', 'readable_date_requires_source_support', actual=date)
        if candidate is not None and not row_issues:
            accepted[str(index)] = {**{k: deepcopy(v) for k,v in candidate.items() if k not in {'status','time_expression'}}, 'content': decision.content,
                'target_id': decision.target_id, 'fallback_reason': None,
                'operation': decision.operation}
            accepted_refs.add(decision.ref)
    for candidate in state.get('memory_candidates', []):
        if candidate['ref'] in accepted_refs:
            continue
        if not any(issue.get('candidate_ref') == candidate['ref'] for issue in issues):
            issues.append(dict(path=['items'], candidate_ref=candidate['ref'], code='candidate_omitted_by_model',
                requirement='Return one decision for this candidate.',
                allowed_target_ids=candidate_targets.get(candidate['ref'], []),
                allowed_decisions=[dict(operation='add', target_id=None), *[
                    dict(operation='update', target_id=value)
                    for value in candidate_targets.get(candidate['ref'], []) if value not in protected]]))
    return accepted, issues


def candidates(accepted, state):
    result = []
    for row in accepted.values():
        citations = sorted(row['evidence'], key=lambda evidence: evidence['source_id'] != state['new_user_source']['id'])
        if any(evidence['source_id'] not in state['source_catalog'] for evidence in citations):
            raise ProviderUnavailable('memory_evidence_requires_supplied_source')
        result.append(Candidate(id=row['ref'], connection=None, content=row['content'], subject_refs=row['subject_refs'],
            record_type=row['record_type'], mode=row['mode'], context=row.get('context'), status=row.get('status'),
            time_expression=row.get('time_expression'), evidence=[EvidenceRef(
                source_id=evidence['source_id'], quote=state['source_catalog'][evidence['source_id']]['content'])
                for evidence in citations]).model_dump())
    return result


def accept_targetless_adds(accepted, issues, state):
    """Resolve candidates with no legal update target as program-owned adds."""
    accepted = deepcopy(accepted)
    targets = eligible_targets(state)
    accepted_refs = {row['ref'] for row in accepted.values()}
    forced = set()
    for candidate in state.get('memory_candidates', []):
        ref = candidate['ref']
        if ref in accepted_refs or targets.get(ref):
            continue
        accepted['program:add:' + ref] = {
            **deepcopy(candidate), 'content': candidate['content'], 'target_id': None,
            'fallback_reason': 'no_eligible_update_target', 'operation': 'add',
        }
        forced.add(ref)
    remaining = [issue for issue in issues if issue.get('candidate_ref') not in forced]
    return accepted, remaining, forced


def pending_candidates(rows, state):
    """Keep unresolved memory decisions searchable without creating a memory record."""
    refs = {row['ref'] for row in rows}
    source_catalog = state['source_catalog']
    result = []
    for row in state.get('memory_candidates', []):
        if row['ref'] not in refs:
            continue
        evidence = [EvidenceRef(source_id=item['source_id'], quote=source_catalog[item['source_id']]['content'])
                    for item in row['evidence'] if item['source_id'] in source_catalog]
        if evidence:
            result.append(Candidate(id=row['ref'], connection=None, content=row['content'],
                subject_refs=row['subject_refs'], record_type=row['record_type'], mode=row['mode'],
                context=row.get('context'), status=row.get('status'),
                time_expression=row.get('time_expression'), evidence=evidence))
    return result


def materialize(store, state, bindings, accepted):
    owner = state['owner']
    primary = state['new_user_source']
    observed = maintenance_clock(state)
    user_id = stable_id('obj_', owner, 'user')
    history = {memory['id']: memory for memory in state.get('history', [])}
    memories, edges, expected, pending, dispositions = [], [], {}, [], []
    identity_scope = maintenance_identity_scope(state)
    items = {row['ref']: MaintainedItem.model_validate({k:v for k,v in row.items() if k not in {'status','time_expression'}}) for row in accepted.values()}
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
                          status=None, planned_at=None, event_at=None, time_evidence=[], time_resolution='unknown', time_precision='unknown',
                          entity_ids=list(dict.fromkeys([*previous.entity_ids, *current_ids])))
        else:
            mid = stable_id('mem_', owner, primary['id'], identity_scope, ref)
            expected[mid] = None
            memory = None
            values = dict(content=item.content, entity_ids=current_ids, mode=item.mode,
                context=item.context or 'source_reported',
                status=None,
                planned_at=None, event_at=None, time_resolution='unknown', time_precision='unknown', time_evidence=[])
        values['content'] = readable_time(values['content'], primary['content'], primary.get('message_at'))
        if previous:
            fields = [('content', 'content'), ('entity_ids', 'participants'), ('mode', 'mode'), ('context', 'context')]
            changed = [label for field, label in fields if values[field] != getattr(previous, field)]
        else:
            changed = ['content', 'participants', 'mode', 'context']
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
            values={key: deepcopy(values[key]) for key in ('content', 'entity_ids', 'mode', 'context')},
            participant_mode='add' if previous else 'replace', participant_ids=current_ids,
            evidence=[entry.model_dump() for entry in patch_evidence], evidence_mode=item.mode,
            source_id=primary['id'], base_revision_id=previous.id if previous else None)
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
    from .revision_replay import replay_revisions
    memories, edges = replay_revisions(store, memories, edges)
    return list(bindings['entity_rows']), memories, edges, expected, pending, dispositions
