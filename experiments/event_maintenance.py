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
from remi.models import MemoryType, ExpressionMode, EventStatus, SourceCitation, StrictModel, Candidate, EvidenceRef, Memory, Revision
from remi.identity import stable_id
from remi.clock import resolve_time, absolute_content, utcnow, RELATIVE_DAYS
from remi.heat import visible_memory
from remi.providers import OutputInvalid, ProviderUnavailable, strict_schema, error_label, remaining, transient
from experiments.subject_extraction import ParticipantHandle, prepare, render
from experiments.maintenance_clock import maintenance_clock, maintenance_identity_scope

class TimeDecision(StrictModel):
    action: Literal['retain','set','clear']
    expression: str | None = Field(max_length=300)

class LinkDecision(StrictModel):
    add_existing: list[str] = Field(max_length=8)
    add_current: list[str] = Field(max_length=8)
    remove_existing: list[str] = Field(max_length=8)

class EventItem(StrictModel):
    ref: str = Field(pattern=r'^C[1-9][0-9]*$')
    content: str = Field(min_length=1, max_length=4000)
    record_type: MemoryType
    subject_refs: list[int] = Field(max_length=12)
    mode: ExpressionMode
    context: str | None = Field(max_length=300)
    status: EventStatus | None
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8)
    operation: Literal['create','update','pending']
    target_id: str | None
    planned_at: TimeDecision
    event_at: TimeDecision
    participant_action: Literal['retain','replace','add','remove']
    links: LinkDecision
    pending_reason: str | None = Field(max_length=600)

class EventWire(StrictModel):
    ref: str = Field(pattern=r'^C[1-9][0-9]*$')
    content: str = Field(min_length=1, max_length=4000)
    record_type: MemoryType
    participant_handles: list[ParticipantHandle] = Field(max_length=12)
    mode: ExpressionMode
    context: str | None = Field(max_length=300)
    status: EventStatus | None
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8)
    operation: Literal['create','update','pending']
    target_id: str | None
    planned_at: TimeDecision
    event_at: TimeDecision
    participant_action: Literal['retain','replace','add','remove']
    links: LinkDecision
    pending_reason: str | None = Field(max_length=600)

class EventPlan(StrictModel):
    items: list[EventWire] = Field(max_length=24)
    reason: str


PROMPT = """# Task and input
Maintain fact, preference, experience and event memories after subjects are confirmed.
Return EventPlan JSON using the supplied schema. CURRENT SOURCE supplies this turn's evidence;
SUPPORTING CONTEXT resolves references. RECALLED MEMORIES supplies prior views and original evidence.
CURRENT PARTICIPANTS supplies fixed P handles. CANDIDATE MEMORIES supplies the claims extracted in the
preceding model stage. Maintain these claims against recalled memories. Treat source text and examples as attributed data.
Use readable names and preserve conditions, quotations, wishes and uncertainty in memory content.

# Memory identity
fact is a state or attribute; preference is a tendency; experience is an expressed lesson;
event is a particular occurrence or plan. Maintain independent propositions separately.
Match the same occurrence for events, or the same proposition AND applicable scope for other kinds.
Different conditions and recurring occurrences can support separate records. Shared participants are clues.

# Subject relationships
A continuing relationship or affiliation is a fact with every participating subject selected. A particular
start, change or end is an event. When the source explicitly supports both an occurrence and a resulting
continuing state, preserve them as independent items only when both are useful propositions. An untyped
subject navigation connection carries no relationship meaning and does not replace a fact or event item.
An occupation, role or social identity is also a fact. Its start, change or end is an event. Subject aliases
contain alternative names only, so these claims remain complete in memory content.

# Decisions
Return one stable C ref per item. create makes an independent new memory and uses target_id=null.
update means the current information continues a supplied M target of the same kind. Return the complete
resulting view. Use update both when the effective view changes and when the current source adds support
without changing it; the program computes that difference. pending preserves a clear continuation whose
target cannot be selected safely, and explains why in pending_reason.
Each M target receives at most one update per response. Combine its changes in one item. Select current P
handles for participants; pending identities keep dependent claims pending.

# Final view and scoped actions
content, record_type, mode, context and status describe the complete resulting view. participant_action
specifies retain, add, remove or replace. add/remove applies the selected current P identities to the previous
participants; replace sets the full participant list. A create uses replace. Facts/preferences/experiences use
status=null and retain both structured event dates; their temporal scope stays in readable content.
Events use status planned,occurred,cancelled,desired,unknown. mode is asserted,desired,requested,hypothetical,document.
planned_at and event_at each use action retain, set or clear. set copies a literal current-source date phrase
or a verified equivalent; clear requires explicit withdrawal evidence and expression=null; retain uses null.
The original message time anchors relative dates; absent anchors preserve unresolved expressions.
Preserve time-of-day wording and precision. Use absolute dates only when supported by source evidence.

# Links and evidence
links.add_existing selects supplied event M targets. links.add_current selects other C refs in this response.
links.remove_existing retracts existing event connections. Empty lists retain existing links. Similar topics
alone support no connection. Every link must be directly supported by CURRENT SOURCE.
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
                      'Return EventPlan JSON with C refs, create/update/pending decisions, final views, scoped actions, M targets and S evidence.')
    if packet.get('memory_candidates'):
        rows=['# CANDIDATE MEMORIES']
        for item in packet['memory_candidates']:
            rows.append(f"{item['ref']} | kind: {item['record_type']} | claim: {item['content']} | participants: {item['participant_handles']}")
        body+='\n\n'+'\n'.join(rows)
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
        links = row.get('links')
        if isinstance(links, dict):
            for field in ('add_existing','remove_existing'):
                related = links.get(field)
                if isinstance(related, list):
                    links[field] = [mapped(mid, context['memory_aliases']) for mid in related]
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
    if state.get('memory_candidates'):
        packet['memory_candidates']=[dict(ref=row['ref'],record_type=row['record_type'],content=row['content'],
            participant_handles=[ctx['ref_handles'].get(ref,'(unavailable participant)') for ref in row['subject_refs']])
            for row in state['memory_candidates']]
    schema = strict_schema(EventPlan)
    props = schema['$defs']['EventWire']['properties']
    handles = list(ctx['handle_refs']); mids = list(ctx['memory_aliases'].values())
    if handles: props['participant_handles']['items']['enum'] = handles
    else: props['participant_handles']['maxItems'] = 0
    schema['$defs']['SourceCitation']['properties']['source_id']['enum'] = packet['source_ids']
    candidate_refs=[row['ref'] for row in packet.get('memory_candidates',[])]
    if candidate_refs:props['ref']['enum']=candidate_refs
    # Nullable target remains nullable; runtime validation checks kind and scope.
    link_props = schema['$defs']['LinkDecision']['properties']
    for field in ('add_existing','remove_existing'):
        if mids: link_props[field]['items']['enum'] = mids
        else: link_props[field]['maxItems'] = 0
    messages = [dict(role='system',content=PROMPT)]
    for example, answer in examples():
        ep, _ = prepare(example)
        ep['memory_candidates']=[dict(ref=row['ref'],record_type=row['record_type'],content=example['new_user_source']['content'],
            participant_handles=row['participant_handles']) for row in answer['items']]
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
                links=row.get('links')
                if isinstance(links,dict):
                    for field in ('add_existing','remove_existing'):
                        related=links.get(field,[])
                        if isinstance(related,list):links[field]=[reverse.get(mid,mid) if isinstance(mid,str) else mid for mid in related]
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
        return dict(new_user_source=source,context_sources=[],resolved_subjects=[dict(ref=0,id='person',name='Mira',kind='person',status='resolved',aliases=[])],history=list(history),subject_catalog=[dict(id='person',name='Mira')])
    old=dict(id='old',record_type='event',content='Mira plans a studio visit on June 3.',entity_ids=['person'],status='planned',planned_at='2029-06-03T00:00:00+00:00',sources=[dict(id='before',role='user',content='Mira plans a studio visit on June 3.',message_at='2029-05-31T10:00:00+00:00')])
    values=[(state('Mira plans another studio visit on June 8.',[old]),dict(content='Mira plans another studio visit on 2029-06-08.',status='planned',planned_at=dict(action='set',expression='June 8'))),
            (state('Mira moved that studio visit to June 4.',[old]),dict(operation='update',target_id='M1',content='Mira plans the studio visit on 2029-06-04.',status='planned',planned_at=dict(action='set',expression='June 4'),participant_action='retain')),
            (state('Mira completed that studio visit today.',[old]),dict(operation='update',target_id='M1',content='Mira completed the studio visit on 2029-06-01.',status='occurred',event_at=dict(action='set',expression='today'),participant_action='retain'))]
    values.append((state('Mira added a ceramics demonstration to that studio visit.',[old]),
        dict(operation='update',target_id='M1',content='Mira plans a studio visit on 2029-06-03, including a ceramics demonstration.',
             status='planned',participant_action='retain')))
    values.append((state('Mira says the workshop was planned for June 3 and actually happened on June 5.',[old]),
        dict(operation='update',target_id='M1',content='Mira attended the workshop on 2029-06-05, originally planned for 2029-06-03.',
            status='occurred',planned_at=dict(action='set',expression='June 3'),
            event_at=dict(action='set',expression='June 5'),participant_action='retain')))
    orphan={**old,'entity_ids':[],'content':'A studio visit is planned.'}
    values.append((state('Mira will host that studio visit.',[orphan]),dict(operation='update',target_id='M1',
        content='Mira will host the studio visit.',status='planned',participant_action='add')))
    preference={**old,'record_type':'preference','content':'Mira prefers written agendas at work.',
        'status':'stated','planned_at':None,'sources':[dict(id='before',role='user',content='Mira prefers written agendas at work.',message_at='2029-05-31T10:00:00+00:00')]}
    values.append((state('Mira now prefers spoken agendas at work.',[preference]),dict(operation='update',
        target_id='M1',record_type='preference',status=None,content='Mira prefers spoken agendas at work.',
        context='at work',participant_action='retain')))
    values.append((state('Mira still prefers written agendas at work.',[preference]),dict(operation='update',
        target_id='M1',record_type='preference',status=None,content=preference['content'],participant_action='retain')))
    for packet,updates in values:
        row=dict(ref='C1',content='',record_type='event',participant_handles=['P0'],mode='asserted',context=None,status='unknown',
            evidence=[dict(source_id='S1')],operation='create',target_id=None,
            planned_at=dict(action='retain',expression=None),event_at=dict(action='retain',expression=None),
            participant_action='replace',links=dict(add_existing=[],add_current=[],remove_existing=[]),pending_reason=None)
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
    declared=Counter(row.get('ref') for row in rows if isinstance(row,dict) and isinstance(row.get('ref'),str))
    row_by_ref={row.get('ref'):row for row in rows if isinstance(row,dict) and declared[row.get('ref')]==1}
    targets=Counter(row.get('target_id') for index,row in enumerate(rows) if str(index) not in protected and isinstance(row,dict) and row.get('operation')=='update' and isinstance(row.get('target_id'),str))
    for index,row in enumerate(rows):
        if str(index) in protected:continue
        def fail(field,code): issues.append(dict(path=['items',index,field],code=code))
        before=len(issues)
        try:item=EventItem.model_validate(row)
        except ValidationError as error:
            issues.extend(dict(path=['items',index,*e['loc']],code=e['type']) for e in error.errors());continue
        if declared[item.ref]!=1:fail('ref','use_one_unique_current_item_ref')
        if not set(item.subject_refs)<=refs or len(item.subject_refs)!=len(set(item.subject_refs)):fail('subject_refs','choose_unique_current_participants')
        ids=[e.source_id for e in item.evidence]
        if state.get('recovery') and item.record_type!=state['recovery']['claim_type']:fail('record_type','recover_the_selected_claim_kind')
        if state.get('recovery') and state['context_sources'][-1]['id'] not in ids:fail('evidence','include_clarification_for_recovery')
        if primary['id'] not in ids or not set(ids)<=sources.keys():fail('evidence','include_current_source_and_only_supplied_sources')
        if item.operation=='update':
            old=history.get(item.target_id)
            if old is None or old['record_type']!=item.record_type:fail('target_id','update_requires_recalled_same_kind')
            if targets[item.target_id]!=1:fail('target_id','combine_changes_to_one_event')
            if item.pending_reason is not None:fail('pending_reason','resolved_update_uses_null_pending_reason')
        elif item.operation=='create':
            if item.target_id is not None:fail('target_id','new_record_uses_null_target')
            if item.participant_action!='replace':fail('participant_action','create_uses_complete_participant_replacement')
            if item.links.remove_existing:fail('links','create_cannot_remove_existing_links')
            if item.pending_reason is not None:fail('pending_reason','resolved_create_uses_null_pending_reason')
        elif item.operation=='pending' and item.target_id is not None and item.target_id not in history:fail('target_id','pending_target_requires_supplied_memory')
        if item.operation=='pending' and not item.pending_reason:fail('pending_reason','pending_requires_specific_reason')
        if item.record_type!='event' and (item.status is not None or item.planned_at.action!='retain' or item.event_at.action!='retain' or any(item.links.model_dump().values())):fail('record_type','statement_time_and_links_belong_in_content')
        if item.record_type=='event' and item.status is None:fail('status','event_requires_explicit_status')
        for field in ('planned_at','event_at'):
            slot=getattr(item,field)
            if slot.action=='retain' and slot.expression is not None:fail(field,'retain_uses_null_expression')
            if slot.action=='clear' and slot.expression is not None:fail(field,'clear_uses_null_expression')
            if slot.action=='set':
                if not slot.expression:fail(field,'set_requires_current_date_expression')
                else:
                    original=original_time_expression(slot.expression,primary)
                    if original is None:fail(field,'date_requires_literal_or_verified_equivalent_current_expression')
                    else:slot.expression=original
        if item.operation!='update' and item.participant_action in {'add','remove'}:fail('participant_action','relative_participant_actions_require_update')
        if item.participant_action=='retain' and item.subject_refs:fail('subject_refs','retain_participants_uses_empty_selection')
        added=set(item.links.add_existing);removed=set(item.links.remove_existing)
        if added&removed:fail('links','add_and_remove_existing_sets_are_disjoint')
        if item.links.remove_existing and item.operation!='update':fail('links','link_retraction_requires_update')
        if item.links.remove_existing and not removed<=set(history.get(item.target_id,{}).get('related_memory_ids',[])):fail('links','remove_only_existing_event_links')
        # Check explicit complete dates in readable content against current/retained event evidence.
        relevant=[primary]
        if item.operation=='update' and item.target_id in history:
            relevant.extend(history[item.target_id]['sources'])
            old=history[item.target_id]
            structural_change=item.status!=old['status'] or item.planned_at.action!='retain' or item.event_at.action!='retain'
            if structural_change and item.content==old['content']:
                fail('content','reflect_changed_status_or_date_in_readable_event_content')
        for date in re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)',item.content):
            if not any(resolve_time(date,source['content'],source.get('message_at')) for source in relevant):
                issues.append(dict(path=['items',index,'content'],code='readable_date_requires_source_support',
                    actual=date,requirement='Use a date supported by the current source or retained event evidence; preserve unresolved expressions.'))
        for mid in [*item.links.add_existing,*item.links.remove_existing]:
            if mid not in history or history[mid]['record_type']!='event' or mid==item.target_id:fail('links','choose_other_recalled_events')
        for ref in item.links.add_current:
            target=row_by_ref.get(ref)
            if ref==item.ref or target is None or target.get('record_type')!='event' or target.get('operation')=='pending':
                fail('links','choose_other_materializable_current_events')
        if len(issues)==before:accepted[str(index)]=item.model_dump()
    return accepted,issues


def candidates(accepted,state):
    result=[]
    for index,row in accepted.items():
        citations=sorted(row['evidence'],key=lambda e:e['source_id']!=state['new_user_source']['id'])
        result.append(Candidate(id=row['ref'],connection=None,content=row['content'],subject_refs=row['subject_refs'],
            record_type=row['record_type'],mode=row['mode'],context=row['context'],status=row['status'],time_expression=None,
            evidence=[EvidenceRef(source_id=e['source_id'],quote=state['source_catalog'][e['source_id']]['content']) for e in citations]).model_dump())
    return result


def materialize(store,state,bindings,accepted):
    owner=state['owner'];primary=state['new_user_source'];observed=maintenance_clock(state)
    user_id=stable_id('obj_',owner,'user'); history={m['id']:m for m in state.get('history',[])}
    memories=[];edges=[];expected={};pending=[];dispositions=[]
    identity_scope=maintenance_identity_scope(state)
    items={row['ref']:EventItem.model_validate(row) for row in accepted.values()}
    candidate_rows={row['id']:row for row in candidates(accepted,state)}
    unresolved_refs={ref for ref,item in items.items() if item.operation=='pending' or any(str(subject_ref) not in bindings['subject_ids'] for subject_ref in item.subject_refs)}
    while True:
        dependent={ref for ref,item in items.items() if set(item.links.add_current)&unresolved_refs}
        if dependent<=unresolved_refs:break
        unresolved_refs.update(dependent)
    ref_memory_ids={ref:(item.target_id if item.operation=='update' else stable_id('mem_',owner,primary['id'],identity_scope,ref))
                    for ref,item in items.items() if ref not in unresolved_refs}
    for ref,item in items.items():
        c=Candidate.model_validate(candidate_rows[ref])
        unresolved=any(str(ref) not in bindings['subject_ids'] for ref in item.subject_refs)
        unavailable_links=set(item.links.add_current)-ref_memory_ids.keys()
        if item.operation=='pending' or unresolved or unavailable_links:
            pending.append(c);dispositions.append(dict(candidate_id=c.id,status='pending',operation='pending',
                reason='candidate_identity_unresolved' if unresolved else item.pending_reason or 'linked_current_memory_unavailable'));continue
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
            mid=memory.id
        else:
            mid=ref_memory_ids[ref]
            expected[mid]=None
            values=dict(content=item.content,entity_ids=ids,mode=item.mode,context=item.context or 'source_reported',status=item.status if item.record_type=='event' else 'stated',planned_at=None,event_at=None,time_resolution='unknown',time_precision='unknown',time_evidence=[])
            memory=None
        if previous:
            values.update(content=item.content,mode=item.mode,context=item.context or 'source_reported',status=item.status if item.record_type=='event' else 'stated')
            old_ids=list(previous.entity_ids)
            values['entity_ids']=old_ids if item.participant_action=='retain' else list(dict.fromkeys([*old_ids,*ids])) if item.participant_action=='add' else [oid for oid in old_ids if oid not in ids] if item.participant_action=='remove' else ids
        for field in ('planned_at','event_at'):
            slot=getattr(item,field)
            if slot.action=='retain':continue
            anchor=primary.get('message_at')
            stamp=resolve_time(slot.expression,primary['content'],anchor) if slot.action=='set' else None
            precision='instant' if stamp and slot.expression and 'T' in slot.expression else 'day' if stamp else 'unknown'
            values[field]=stamp
            values['time_resolution']='rule' if stamp else 'unknown';values['time_precision']=precision
            details=[t for t in values.get('time_evidence',[]) if t['field']!=field]
            details.append(dict(field=field,expression=slot.expression,source_id=primary['id'],anchor=anchor,value=stamp,precision=precision))
            values['time_evidence']=details
        # Only current expressions are rewritten; retained historical text uses its original anchors.
        values['content']=readable_time(values['content'],primary['content'],primary.get('message_at'))
        changed=[]
        if previous:
            for field,key in [('content','content'),('entity_ids','participants'),('mode','mode'),('context','context'),('status','status'),('planned_at','planned_at'),('event_at','event_at')]:
                if values[field]!=getattr(previous,field):changed.append(key)
        else:changed=['content','participants','mode','context','status','planned_at','event_at']
        previous_links=set(history.get(item.target_id,{}).get('related_memory_ids',[])) if previous else set()
        requested_add=[*item.links.add_existing,*[ref_memory_ids[current] for current in item.links.add_current]]
        related_add=[mid for mid in dict.fromkeys(requested_add) if mid not in previous_links]
        related_remove=[mid for mid in dict.fromkeys(item.links.remove_existing) if mid in previous_links]
        support=bool(previous) and not changed and not related_add and not related_remove
        values.update(id=stable_id('rev_',mid,primary['id'],identity_scope,c.id),source_ids=[e.source_id for e in evidence],evidence=[e.model_dump() for e in evidence],quote=c.evidence[0].quote,
            observed_at=observed,created_at=utcnow(),operation='support' if support else 'update' if previous else 'create',replaces_revision_id=previous.id if previous else None,
            user_involvement='involved' if user_id in values['entity_ids'] else 'not_involved')
        if state.get('recovery'):
            clarification=state['context_sources'][-1]
            evidence.append(EvidenceRef(source_id=clarification['id'],quote=clarification['content']))
            values.update(source_ids=list(dict.fromkeys(e.source_id for e in evidence)),evidence=[e.model_dump() for e in evidence])
        patch_evidence=evidence if state.get('recovery') else c.evidence
        values['maintenance_patch']=dict(fields=changed,
            values={key:deepcopy(values[key]) for key in ('content','entity_ids','mode','context','status','planned_at','event_at','time_evidence')},
            participant_mode=item.participant_action,participant_ids=ids,
            evidence=[e.model_dump() for e in patch_evidence],evidence_mode=item.mode,
            related_add=related_add,related_remove=related_remove,
            source_id=primary['id'],base_revision_id=previous.id if previous else None)
        revision=Revision.model_validate(values)
        if memory:memory.revisions.append(revision);memory=Memory.model_validate(memory.model_dump())
        else:memory=Memory(id=mid,owner=owner,record_type=item.record_type,revisions=[revision])
        memories.append(memory)
        disposition='supported' if support else 'updated' if previous else 'created'
        dispositions.append(dict(candidate_id=c.id,status='accepted',operation=disposition,memory_id=mid,reason='source_validated_memory_maintenance'))
    from experiments.revision_replay import replay_revisions
    memories,edges=replay_revisions(store,memories,edges)
    return list(bindings['entity_rows']),memories,edges,expected,pending,dispositions
