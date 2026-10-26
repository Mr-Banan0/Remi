"""Subject-first memory maintenance: scoped edits, source evidence and immutable event revisions."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
import json
import hashlib
import calendar
import re
from time import monotonic, sleep
from typing import Literal

from pydantic import Field, ValidationError
from remi.models import MemoryType, ExpressionMode, EventStatus, SourceCitation, CandidateDraft, StrictModel, Candidate, EvidenceRef, Memory, Revision, Edge
from remi.identity import stable_id
from remi.clock import resolve_time, absolute_content, utcnow, RELATIVE_DAYS
from remi.heat import visible_memory
from remi.providers import OutputInvalid, ProviderUnavailable, strict_schema, error_label, remaining, transient
from experiments.subject_extraction import ParticipantHandle, prepare, render
from experiments.maintenance_clock import maintenance_clock, maintenance_identity_scope

Change = Literal['content','participants','mode','context','status','planned_at','event_at']

class TimeUpdate(StrictModel):
    field: Literal['planned_at','event_at']
    expression: str | None = Field(max_length=300)

class EventItem(CandidateDraft):
    time_expression: None = None  # Shared Candidate projection; wire protocol uses time_updates.
    operation: Literal['create','update','keep','pending'] = 'create'
    target_id: str | None = None
    changed_fields: list[Change] = Field(default_factory=list)
    time_updates: list[TimeUpdate] = Field(default_factory=list, max_length=2)
    participant_mode: Literal['retain','replace','add','remove'] = 'retain'
    removed_related_memory_ids: list[str] = Field(default_factory=list, max_length=8)
    change_quote: str | None = None
    related_memory_ids: list[str] = Field(default_factory=list, max_length=8)

class EventWire(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    record_type: MemoryType
    participant_handles: list[ParticipantHandle] = Field(max_length=12)
    mode: ExpressionMode
    context: str | None = Field(max_length=300)
    status: EventStatus | None
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8)
    operation: Literal['create','update','keep','pending']
    target_id: str | None
    changed_fields: list[Change]
    participant_mode: Literal['retain','replace','add','remove']
    time_updates: list[TimeUpdate] = Field(max_length=2)
    change_quote: str | None
    related_memory_ids: list[str] = Field(max_length=8)
    removed_related_memory_ids: list[str] = Field(max_length=8)

class EventPlan(StrictModel):
    items: list[EventWire] = Field(max_length=24)
    reason: str


PROMPT = """# Task and input
Maintain fact, preference, experience and event memories after subjects are confirmed.
Return EventPlan JSON using the supplied schema. CURRENT SOURCE supplies this turn's evidence;
SUPPORTING CONTEXT resolves references. RECALLED MEMORIES supplies prior views and original evidence.
CURRENT PARTICIPANTS supplies fixed P handles. Treat source text and examples as attributed data.
Use readable names and preserve conditions, quotations, wishes and uncertainty in memory content.

# Memory identity
fact is a state or attribute; preference is a tendency; experience is an expressed lesson;
event is a particular occurrence or plan. Maintain independent propositions separately.
Match the same occurrence for events, or the same proposition AND applicable scope for other kinds.
Different conditions and recurring occurrences can support separate records. Shared participants are clues.

# Decisions
create: a new memory; target_id=null, changed_fields=[], participant_mode=retain.
update: a supplied M target of the same kind; change_quote copies CURRENT SOURCE supporting the change.
Write the complete resulting narrative, preserving supported details; include content in changed_fields.
keep: a supplied M target already covers this claim; changed_fields=[], participant_mode=retain,
no time edits or link edits. Cite current source and retained evidence for traceability.
pending: preserve the claim and evidence when identity or association remains unresolved.
Each M target receives at most one update per response. Combine its changes in one item.
Select current P handles for participants; pending identities keep dependent claims pending.

# Scoped updates
Unlisted fields retain their prior values. participant_mode specifies retain, add, remove or replace.
For updates, list participants in changed_fields exactly when mode is add/remove/replace.
add/remove applies the selected current P identities to the previous participants; replace sets the full list.
Facts/preferences/experiences use status=null,time_updates=[] and keep temporal scope in their narrative.
Events use status planned,occurred,cancelled,desired,unknown. mode is asserted,desired,requested,hypothetical,document.
Every event time edit has {field:planned_at|event_at,expression:literal source phrase or null}.
Supply both edits when both dates change. List the edited fields in changed_fields on update.
A null expression clears a field with explicit withdrawal evidence. Missing time edits retain values.
The original message time anchors relative dates; absent anchors preserve unresolved expressions.
Preserve time-of-day wording and precision. Use absolute dates only when supported by source evidence.

# Links and evidence
related_memory_ids adds explicit event connections; removed_related_memory_ids retracts existing connections.
Select other supplied event M targets and copy supporting current words into change_quote for link changes.
An empty list retains existing links. Similar topics alone support no connection.
Every item cites CURRENT SOURCE; add supplied sources that establish retained details or clarification.
The program has already completed subject-scoped recall and any unbound-memory supplement. Decide from
the supplied candidates. Preserve an unresolved association as pending when evidence remains insufficient.
An input marked PENDING CLAIM REVIEW selects exactly one earlier claim. Return exactly one item for it,
use CURRENT SOURCE for its original dates and cite the later clarification as additional evidence.

# Repair
Return all schema fields using null or [] where appropriate. Repair listed errors in the same item positions;
accepted items are protected. Subject identity decisions stay fixed during memory maintenance.
"""


def render_event(packet):
    packet=deepcopy(packet)
    if not packet['new_user_source'].get('message_at'):
        packet['new_user_source'].pop('created_at',None)
    body=render(packet)
    body=body.replace('Return the complete items and reason JSON for CURRENT SOURCE using its current P handles.',
                      'Return EventPlan JSON with operations, changed fields, current P participants, M targets and S evidence.')
    if packet.get('recovery'):
        body+='\n# PENDING CLAIM REVIEW\nResolve exactly this prior claim: '+packet['recovery']['content']
    return body


def readable_time(content,source,anchor):
    qualifiers={'this morning':'morning','this afternoon':'afternoon','this evening':'evening',
                'tonight':'night','last night':'night'}
    pattern=r'\b(?:'+ '|'.join(qualifiers)+r')\b'
    def replace(match):
        stamp=resolve_time(match[0],source,anchor)
        return stamp[:10]+' '+qualifiers[match[0].casefold()] if stamp else match[0]
    return absolute_content(re.sub(pattern,replace,content,flags=re.IGNORECASE),source,anchor)


def repair_output(raw, context):
    """Translate well-typed IDs while preserving malformed values for model correction."""
    previous = deepcopy(raw)
    rows = previous.get('items') if isinstance(previous, dict) else None
    if not isinstance(rows, list):
        return previous
    def mapped(value, aliases):
        return aliases.get(value, value) if isinstance(value, str) else value
    for row in rows:
        if not isinstance(row, dict):
            continue
        if 'subject_refs' in row:
            refs = row.pop('subject_refs')
            row['participant_handles'] = [context['ref_handles'].get(ref, ref) if type(ref) is int else ref
                                          for ref in refs] if isinstance(refs, list) else refs
        if 'target_id' in row:
            row['target_id'] = mapped(row['target_id'], context['memory_aliases'])
        for field in ('related_memory_ids','removed_related_memory_ids'):
            related = row.get(field)
            if isinstance(related, list):
                row[field] = [mapped(mid, context['memory_aliases']) for mid in related]
        evidence = row.get('evidence')
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict) and 'source_id' in item:
                    item['source_id'] = mapped(item['source_id'], context['source_aliases'])
    return previous


def memory_fingerprint(record):
    """Compare complete stored history separately from its source-clock projection."""
    normalized = Memory.model_validate(record).model_dump()
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def provider_plan(model, state, *, deadline):
    packet, ctx = prepare(state)
    packet.update({k:deepcopy(state[k]) for k in ('recovery',) if k in state})
    schema = strict_schema(EventPlan)
    props = schema['$defs']['EventWire']['properties']
    handles = list(ctx['handle_refs']); mids = list(ctx['memory_aliases'].values())
    if handles: props['participant_handles']['items']['enum'] = handles
    else: props['participant_handles']['maxItems'] = 0
    schema['$defs']['SourceCitation']['properties']['source_id']['enum'] = packet['source_ids']
    # Nullable target remains nullable; runtime validation checks event type and scope.
    for field in ('related_memory_ids','removed_related_memory_ids'):
        if mids: props[field]['items']['enum'] = mids
        else: props[field]['maxItems'] = 0
    messages = [dict(role='system',content=PROMPT)]
    for example, answer in examples():
        ep, _ = prepare(example)
        messages.extend([dict(role='user',content='# INDEPENDENT EXAMPLE\n'+render_event(ep)),
                         dict(role='assistant',content=json.dumps(answer,ensure_ascii=False))])
    messages.append(dict(role='user',content='# MEMORY MAINTENANCE INPUT\n'+render_event(packet)))
    if state.get('repair_errors'):
        previous = repair_output(state.get('previous_output',{}), ctx)
        messages.append(dict(role='assistant',content=json.dumps(previous,ensure_ascii=False)))
        messages.append(dict(role='user',content='Correct these issues; preserve accepted items and indices:\n'+json.dumps(state['repair_errors'],ensure_ascii=False)))
    request=dict(model=model.model,temperature=0,max_completion_tokens=6500,messages=messages,
        response_format=dict(type='json_schema',json_schema=dict(name='EventPlan',strict=True,schema=schema)))
    for attempt in range(2):
        started=monotonic(); trace=dict(kind='add_propose_repair' if state.get('repair_errors') else 'add_propose',model=model.model,attempt=attempt+1,
            pending_recovery=bool(state.get('recovery')))
        try:
            response=model.client.chat.completions.create(**request,timeout=remaining(deadline,model.timeout))
            if response.usage: trace.update(input_tokens=response.usage.prompt_tokens,output_tokens=response.usage.completion_tokens)
            message=response.choices[0].message
            if not message.content or getattr(message,'refusal',None): raise OutputInvalid('event_response_unavailable')
            raw=json.loads(message.content)
            if not isinstance(raw,dict): raise OutputInvalid('event_plan_requires_object')
            # Decode per row so one invalid sibling can be repaired while valid rows are retained.
            result=deepcopy(raw); reverse={v:k for k,v in ctx['memory_aliases'].items()}
            for row in result.get('items',[]) if isinstance(result.get('items'),list) else []:
                if not isinstance(row,dict): continue
                handles=row.pop('participant_handles',None)
                row['subject_refs']=[ctx['handle_refs'].get(h,-1) if isinstance(h,str) else -1 for h in handles] if isinstance(handles,list) else handles
                target=row.get('target_id');row['target_id']=reverse.get(target,target) if isinstance(target,str) else target
                for field in ('related_memory_ids','removed_related_memory_ids'):
                    related=row.get(field,[])
                    if isinstance(related,list):row[field]=[reverse.get(mid,mid) if isinstance(mid,str) else mid for mid in related]
                for ev in row.get('evidence',[]) if isinstance(row.get('evidence'),list) else []:
                    if isinstance(ev,dict) and isinstance(ev.get('source_id'),str):ev['source_id']=ctx['source_originals'].get(ev['source_id'],ev['source_id'])
            return result
        except (ValueError,OutputInvalid) as error:
            trace.update(error_label(error));raise OutputInvalid('event_response_invalid') from error
        except Exception as error:
            trace.update(error_label(error))
            if attempt==0 and transient(error) and deadline-monotonic()>1:sleep(.2);continue
            raise ProviderUnavailable('event_provider_unavailable') from error
        finally:
            trace['seconds']=monotonic()-started;model.calls.append(trace)
    raise ProviderUnavailable('event_provider_unavailable')


def examples():
    def state(text,history=()):
        source=dict(id='now',role='user',content=text,message_at='2029-06-01T10:00:00+00:00')
        return dict(new_user_source=source,context_sources=[],resolved_subjects=[dict(ref=0,id='person',name='Mira',kind='individual',status='resolved',aliases=[],descriptions=[])],history=list(history),subject_catalog=[dict(id='person',name='Mira')])
    old=dict(id='old',record_type='event',content='Mira plans a studio visit on June 3.',entity_ids=['person'],status='planned',planned_at='2029-06-03T00:00:00+00:00',sources=[dict(id='before',role='user',content='Mira plans a studio visit on June 3.',message_at='2029-05-31T10:00:00+00:00')])
    values=[(state('Mira plans another studio visit on June 8.',[old]),dict(content='Mira plans another studio visit on 2029-06-08.',status='planned',time_updates=[dict(field='planned_at',expression='June 8')])),
            (state('Mira moved that studio visit to June 4.',[old]),dict(operation='update',target_id='M1',content='Mira plans the studio visit on 2029-06-04.',status='planned',time_updates=[dict(field='planned_at',expression='June 4')],changed_fields=['content','planned_at'],change_quote='Mira moved that studio visit to June 4.')),
            (state('Mira completed that studio visit today.',[old]),dict(operation='update',target_id='M1',content='Mira completed the studio visit on 2029-06-01.',status='occurred',time_updates=[dict(field='event_at',expression='today')],changed_fields=['content','status','event_at'],change_quote='Mira completed that studio visit today.'))]
    values.append((state('Mira added a ceramics demonstration to that studio visit.',[old]),
        dict(operation='update',target_id='M1',content='Mira plans a studio visit on 2029-06-03, including a ceramics demonstration.',
             status='planned',changed_fields=['content'],change_quote='Mira added a ceramics demonstration to that studio visit.')))
    values.append((state('Mira says the workshop was planned for June 3 and actually happened on June 5.',[old]),
        dict(operation='update',target_id='M1',content='Mira attended the workshop on 2029-06-05, originally planned for 2029-06-03.',
            status='occurred',changed_fields=['content','status','planned_at','event_at'],
            time_updates=[dict(field='planned_at',expression='June 3'),dict(field='event_at',expression='June 5')],
            change_quote='Mira says the workshop was planned for June 3 and actually happened on June 5.')))
    orphan={**old,'entity_ids':[],'content':'A studio visit is planned.'}
    values.append((state('Mira will host that studio visit.',[orphan]),dict(operation='update',target_id='M1',
        content='Mira will host the studio visit.',status='planned',changed_fields=['content','participants'],
        participant_mode='add',change_quote='Mira will host that studio visit.')))
    preference={**old,'record_type':'preference','content':'Mira prefers written agendas at work.',
        'status':'stated','planned_at':None,'sources':[dict(id='before',role='user',content='Mira prefers written agendas at work.',message_at='2029-05-31T10:00:00+00:00')]}
    values.append((state('Mira now prefers spoken agendas at work.',[preference]),dict(operation='update',
        target_id='M1',record_type='preference',status=None,content='Mira prefers spoken agendas at work.',
        context='at work',changed_fields=['content','context'],change_quote='Mira now prefers spoken agendas at work.')))
    values.append((state('Mira still prefers written agendas at work.',[preference]),dict(operation='keep',
        target_id='M1',record_type='preference',status=None,content=preference['content'])))
    for packet,updates in values:
        row=dict(content='',record_type='event',participant_handles=['P0'],mode='asserted',context=None,status='unknown',evidence=[dict(source_id='S1')],operation='create',target_id=None,changed_fields=[],time_updates=[],participant_mode='retain',change_quote=None,related_memory_ids=[],removed_related_memory_ids=[])
        row.update(updates)
        yield packet,dict(items=[EventWire.model_validate(row).model_dump()],reason='Current evidence identifies the occurrence and supported changes.')


def original_time_expression(expression,primary):
    """Accept a verified normalization and recover the matching phrase from current evidence."""
    if not expression:return expression
    text=primary['content'];anchor=primary.get('message_at')
    literal=re.search(re.escape(expression),text,re.IGNORECASE)
    if literal:return literal[0]
    expected=resolve_time(expression,text,anchor)
    if not expected:return None
    months='|'.join(sorted({name for i in range(1,13) for name in (calendar.month_name[i],calendar.month_abbr[i])}|{'Sept'},key=len,reverse=True))
    patterns=[r'\d{4}-\d{2}-\d{2}(?:T[0-9:.+Z-]+)?',r'(?:\d{4}年)?\d{1,2}月\d{1,2}日',
        rf'\b(?:{months})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b',
        rf'\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{months})\.?(?:,?\s+\d{{4}})?\b',
        '|'.join(re.escape(x) for x in sorted(RELATIVE_DAYS,key=len,reverse=True))]
    for pattern in patterns:
        for match in re.finditer(pattern,text,re.IGNORECASE):
            if resolve_time(match[0],text,anchor)==expected:return match[0]
    return None


def validate_plan(raw,state,view,*,protected=None):
    """Return independent accepted items and actionable field errors; semantics remain model work."""
    protected=protected or {}
    accepted={}; issues=[]; rows=raw.get('items') if isinstance(raw,dict) else None
    if not isinstance(rows,list) or len(rows)>24:
        return {},[dict(path=['items'],code='event_plan_requires_at_most_24_items')]
    if state.get('recovery') and len(rows)!=1:
        return {},[dict(path=['items'],code='recovery_requires_exactly_one_claim')]
    refs={p['ref'] for p in view}; history={m['id']:m for m in state.get('history',[])}
    primary=state['new_user_source']; sources=state['source_catalog']
    targets=Counter(row.get('target_id') for index,row in enumerate(rows) if str(index) not in protected and isinstance(row,dict) and row.get('operation')=='update' and isinstance(row.get('target_id'),str))
    for index,row in enumerate(rows):
        if str(index) in protected:continue
        def fail(field,code): issues.append(dict(path=['items',index,field],code=code))
        before=len(issues)
        try:item=EventItem.model_validate(row)
        except ValidationError as error:
            issues.extend(dict(path=['items',index,*e['loc']],code=e['type']) for e in error.errors());continue
        if not set(item.subject_refs)<=refs or len(item.subject_refs)!=len(set(item.subject_refs)):fail('subject_refs','choose_unique_current_participants')
        ids=[e.source_id for e in item.evidence]
        if state.get('recovery') and item.record_type!=state['recovery']['claim_type']:fail('record_type','recover_the_selected_claim_kind')
        if state.get('recovery') and state['context_sources'][-1]['id'] not in ids:fail('evidence','include_clarification_for_recovery')
        if primary['id'] not in ids or not set(ids)<=sources.keys():fail('evidence','include_current_source_and_only_supplied_sources')
        if item.operation=='update':
            old=history.get(item.target_id)
            if old is None or old['record_type']!=item.record_type:fail('target_id','update_requires_recalled_same_kind')
            if targets[item.target_id]!=1:fail('target_id','combine_changes_to_one_event')
            if not item.changed_fields or len(item.changed_fields)!=len(set(item.changed_fields)):fail('changed_fields','list_unique_fields_changed_by_current_source')
            if 'content' not in item.changed_fields:fail('changed_fields','include_readable_resulting_event_content')
        elif item.operation=='keep':
            old=history.get(item.target_id)
            if old is None or old['record_type']!=item.record_type:fail('target_id','keep_requires_recalled_same_kind')
            if item.changed_fields or item.time_updates or item.related_memory_ids or item.removed_related_memory_ids or item.participant_mode!='retain':fail('operation','keep_has_no_edits')
        elif item.operation=='create' and (item.target_id is not None or item.changed_fields):fail('target_id','new_record_uses_null_target_and_empty_changes')
        elif item.operation=='pending' and item.target_id is not None and item.target_id not in history:fail('target_id','pending_target_requires_supplied_memory')
        if item.operation=='update' or item.related_memory_ids or item.removed_related_memory_ids:
            if not item.change_quote or item.change_quote not in primary['content']:fail('change_quote','copy_current_words_supporting_update_or_link')
        if item.record_type!='event' and (item.status is not None or item.time_updates or item.related_memory_ids or item.removed_related_memory_ids):fail('record_type','statement_time_scope_belongs_in_content')
        if item.record_type!='event' and set(item.changed_fields)&{'status','planned_at','event_at'}:fail('changed_fields','statement_edits_use_content_participants_mode_context')
        if item.record_type=='event' and item.status is None:fail('status','event_requires_explicit_status')
        if item.time_expression is not None:fail('time_expression','use_time_updates')
        if len({t.field for t in item.time_updates})!=len(item.time_updates):fail('time_updates','one_edit_per_time_field')
        for slot in item.time_updates:
            if slot.expression:
                original=original_time_expression(slot.expression,primary)
                if original is None:fail('time_updates','date_requires_literal_or_verified_equivalent_current_expression')
                else:slot.expression=original
        if item.operation=='update':
            if ('participants' in item.changed_fields)!=(item.participant_mode!='retain'):
                fail('participant_mode','list_participants_when_editing_membership')
        elif item.participant_mode!='retain':fail('participant_mode','membership_edits_require_update')
        if set(item.related_memory_ids)&set(item.removed_related_memory_ids):fail('related_memory_ids','add_and_remove_sets_are_disjoint')
        if item.removed_related_memory_ids and item.operation!='update':fail('removed_related_memory_ids','retraction_requires_update')
        if item.removed_related_memory_ids and not set(item.removed_related_memory_ids)<=set(history.get(item.target_id,{}).get('related_memory_ids',[])):fail('removed_related_memory_ids','select_existing_event_links')
        # Check explicit complete dates in readable content against current/retained event evidence.
        relevant=[primary]
        if item.operation=='update' and item.target_id in history:
            relevant.extend(history[item.target_id]['sources'])
            old=history[item.target_id]
            structural_change=('status' in item.changed_fields and item.status!=old['status']) or bool(set(item.changed_fields)&{'planned_at','event_at'})
            if structural_change and item.content==old['content']:
                fail('content','reflect_changed_status_or_date_in_readable_event_content')
        for date in re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)',item.content):
            if not any(resolve_time(date,source['content'],source.get('message_at')) for source in relevant):
                issues.append(dict(path=['items',index,'content'],code='readable_date_requires_source_support',
                    actual=date,requirement='Use a date supported by the current source or retained event evidence; preserve unresolved expressions.'))
        if item.operation=='update':
            date_fields=set(item.changed_fields)&{'planned_at','event_at'}
            if date_fields!={t.field for t in item.time_updates}:fail('time_updates','list_exactly_the_changed_date_fields')
        for mid in [*item.related_memory_ids,*item.removed_related_memory_ids]:
            if mid not in history or history[mid]['record_type']!='event' or mid==item.target_id:fail('related_memory_ids','choose_other_recalled_events')
        if len(issues)==before:accepted[str(index)]=item.model_dump()
    return accepted,issues


def candidates(accepted,state):
    result=[]
    for index,row in accepted.items():
        fields=CandidateDraft.model_validate({k:row[k] for k in CandidateDraft.model_fields}).model_dump(exclude={'evidence'})
        citations=sorted(row['evidence'],key=lambda e:e['source_id']!=state['new_user_source']['id'])
        result.append(Candidate(id=index,connection=None,**fields,evidence=[EvidenceRef(source_id=e['source_id'],quote=state['source_catalog'][e['source_id']]['content']) for e in citations]).model_dump())
    return result


def materialize(store,state,bindings,accepted):
    owner=state['owner'];primary=state['new_user_source'];observed=maintenance_clock(state)
    user_id=stable_id('obj_',owner,'user'); history={m['id']:m for m in state.get('history',[])}
    memories=[];edges=[];expected={};pending=[];dispositions=[]
    identity_scope=maintenance_identity_scope(state)
    for candidate in candidates(accepted,state):
        item=EventItem.model_validate(accepted[candidate['id']]); c=Candidate.model_validate(candidate)
        unresolved=any(str(ref) not in bindings['subject_ids'] for ref in item.subject_refs)
        if item.operation=='pending' or unresolved:
            pending.append(c);dispositions.append(dict(candidate_id=c.id,status='pending',operation='pending',reason='candidate_identity_unresolved' if unresolved else 'event_association_unresolved'));continue
        if item.operation=='keep':
            dispositions.append(dict(candidate_id=c.id,status='accepted',operation='keep',memory_id=item.target_id,reason='existing_memory_covers_claim'));continue
        evidence=list(c.evidence); ids=list(dict.fromkeys(bindings['subject_ids'][str(ref)] for ref in item.subject_refs))
        previous=None
        if item.operation=='update':
            raw=store.get('memories',owner,item.target_id)
            if raw is None:raise ProviderUnavailable('event_history_changed_retry')
            snapshot=state.get('memory_snapshot_hashes',{}).get(item.target_id)
            if snapshot is not None and memory_fingerprint(raw)!=snapshot:
                raise ProviderUnavailable('event_history_changed_retry')
            memory=Memory.model_validate(raw)
            historical=visible_memory(memory,observed)
            if historical is None or historical.current.id!=history[item.target_id]['revision_id']:
                raise ProviderUnavailable('event_history_changed_retry')
            previous=historical.current
            expected[memory.id]=raw
            evidence=list({e.source_id:e for e in [*evidence,*previous.evidence]}.values())
            values=previous.model_dump()
            for field in item.changed_fields:
                if field in {'content','mode','context','status'}:values[field]=getattr(item,field) if field!='context' else item.context or 'source_reported'
                if field=='participants':
                    old_ids=values['entity_ids']
                    values['entity_ids']=list(dict.fromkeys([*old_ids,*ids])) if item.participant_mode=='add' else [oid for oid in old_ids if oid not in ids] if item.participant_mode=='remove' else ids
            mid=memory.id
        else:
            mid=stable_id('mem_',owner,primary['id'],identity_scope,c.id)
            expected[mid]=None
            values=dict(content=item.content,entity_ids=ids,mode=item.mode,context=item.context or 'source_reported',status=item.status if item.record_type=='event' else 'stated',planned_at=None,event_at=None,time_resolution='unknown',time_precision='unknown',time_evidence=[])
            memory=None
        for slot in item.time_updates:
            field=slot.field
            anchor=primary.get('message_at')
            stamp=resolve_time(slot.expression,primary['content'],anchor)
            precision='instant' if stamp and slot.expression and 'T' in slot.expression else 'day' if stamp else 'unknown'
            values[field]=stamp
            values['time_resolution']='rule' if stamp else 'unknown';values['time_precision']=precision
            details=[t for t in values.get('time_evidence',[]) if t['field']!=field]
            details.append(dict(field=field,expression=slot.expression,source_id=primary['id'],anchor=anchor,value=stamp,precision=precision))
            values['time_evidence']=details
        # Only current expressions are rewritten; retained historical text uses its original anchors.
        values['content']=readable_time(values['content'],primary['content'],primary.get('message_at'))
        values.update(id=stable_id('rev_',mid,primary['id'],identity_scope,c.id),source_ids=[e.source_id for e in evidence],evidence=[e.model_dump() for e in evidence],quote=c.evidence[0].quote,
            observed_at=observed,created_at=utcnow(),operation='update' if previous else 'create',replaces_revision_id=previous.id if previous else None,
            user_involvement='involved' if user_id in values['entity_ids'] else 'not_involved')
        if state.get('recovery'):
            clarification=state['context_sources'][-1]
            evidence.append(EvidenceRef(source_id=clarification['id'],quote=clarification['content']))
            values.update(source_ids=list(dict.fromkeys(e.source_id for e in evidence)),evidence=[e.model_dump() for e in evidence])
        values['maintenance_patch']=dict(fields=list(item.changed_fields) if previous else ['content','participants','mode','context','status','planned_at','event_at'],
            values={key:deepcopy(values[key]) for key in ('content','entity_ids','mode','context','status','planned_at','event_at','time_evidence')},
            participant_mode=item.participant_mode,participant_ids=ids,
            evidence=[e.model_dump() for e in (evidence if state.get('recovery') else c.evidence)],
            related_add=item.related_memory_ids,related_remove=item.removed_related_memory_ids,
            source_id=primary['id'],base_revision_id=previous.id if previous else None)
        revision=Revision.model_validate(values)
        if memory:memory.revisions.append(revision);memory=Memory.model_validate(memory.model_dump())
        else:memory=Memory(id=mid,owner=owner,record_type=item.record_type,revisions=[revision])
        memories.append(memory)
        dispositions.append(dict(candidate_id=c.id,status='accepted',operation=item.operation,memory_id=mid,reason='source_validated_memory_maintenance'))
    from experiments.revision_replay import replay_revisions
    memories,edges=replay_revisions(store,memories,edges)
    return list(bindings['entity_rows']),memories,edges,expected,pending,dispositions
