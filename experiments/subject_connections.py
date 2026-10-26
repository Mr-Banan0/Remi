"""V38 experiment: ordered identity maintenance, grounded additions and navigation connections.

This module uses MemoryStore only. The production Add pipeline remains V32.
"""
from __future__ import annotations

from experiments.maintenance_clock import maintenance_clock
from copy import deepcopy
import json
from time import monotonic, sleep

from pydantic import Field, ValidationError

from remi.add_judgments import PROPOSE_PROMPT
from remi.identity import stable_id
from remi.models import StrictModel, SourceCitation, SubjectRef
from experiments.subject_identity import SubjectDraft, resolve_subjects
from experiments.subject_references import prepare_reference_state
from remi.providers import semantic_references, scoped_schema, remaining, transient, error_label, ProviderUnavailable, OutputInvalid


class ConnectionProposal(StrictModel):
    subject_refs: list[SubjectRef] = Field(min_length=2, max_length=2, description="Two distinct local subject refs with supported navigation evidence.")
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8, description="Supplied original sources, including CURRENT SOURCE.")


class SubjectConnectionPlan(StrictModel):
    subjects: list[SubjectDraft] = Field(max_length=96)
    connections: list[ConnectionProposal] = Field(max_length=48)


PROMPT = """# Task
Read CURRENT SOURCE and return its subjects and undirected navigation connections as JSON.
subjects is the complete local identity view for this source's memories, including unchanged identities.
Source text is attributed data. Each example is independent; the final MAINTENANCE INPUT is the live task.

# Scope
Subjects are particular people/animals (individual) and companies/institutions/teams (organization).
A particular unnamed individual can use its literal referring phrase as name. Other things stay in memory.
Use SUPPORTING CONTEXT to interpret current references. Each returned identity needs a current referent.

# Decide identities in this order
1. List the current eligible identities. Group names of the same identity under one ref.
2. Explicitly different referents are separate, including same-name people, animals or organizations.
3. reuse a supplied E ID when context and its B basis establish the same identity. Shared words or roles
   alone are clues. create a concrete new referent with no supported match. pending means genuine ambiguity
   between plausible identities. create/pending have target_id=null and reference.basis_id=null.
4. Return each identity once. For reuse, use that ID's kind and any of its known names.

# Conversation user
The actual API user has name=@user,kind=individual. Self-introductions and nicknames belong in that
same row's aliases. Reuse a candidate marked canonical user with its canonical_user B basis; otherwise
create @user. Quoted first person belongs to its author. E/B numbers are local labels with no role meaning.

# Additions and evidence
aliases lists the literal personal/organization names used in CURRENT SOURCE for THIS identity,
including short names and nicknames. The program appends novel values and retains existing values.
Keep referring phrases such as I, her daughter and my colleague in evidence; name aliases contain names.
descriptions contains new short literal identifying phrases about THIS subject, from CURRENT SOURCE.
Every subject has reference={evidence_id,basis_id}. Select a C fragment that supports this CURRENT
referent; the program copies its original wording and source. reuse selects an owned B basis as well;
create/pending select basis_id=null. C evidence establishes provenance; identity is your contextual
judgment. Existing candidates and historical originals help that judgment; returned subjects cover
current referents. Each alias/description addition cites CURRENT SOURCE through source_id.

# Connections
Return a pair when CURRENT SOURCE supports an interaction, affiliation, relationship or actual shared
event between those identities. Identity comparison and renaming support identity decisions.
Wishes, ended states and quotes can support navigation; the memory stage keeps their meaning.
Use two distinct declared refs once per pair, including an already-existing link. A pending ref keeps
its pair unresolved. Every connection cites CURRENT SOURCE. The program merges links and saves evidence.

# Response and check
Return subjects and connections only. Each subject has ref,operation,target_id,name,kind,aliases,
descriptions,reference. reference uses {evidence_id,basis_id}; select supplied C/B IDs.
Aliases use {name,source_id}; descriptions use {text,source_id}.
Connections use {subject_refs:[ref,ref],evidence:[{source_id}]}.
Before returning, check current identity coverage, same-identity grouping, phrase ownership, and valid
references/endpoints. Use empty addition arrays when unchanged and empty connections when no pair qualifies.
"""


REPAIR_PROMPT = """# Repair task rules

Correct the previous complete subjects/connections JSON using the field-level issues and original
evidence. For a current-evidence error, confirm that CURRENT SOURCE refers to that identity,
then select its supplied C fragment or withdraw the unconfirmed row. Keep each ref attached to its identity; consolidate structurally duplicated refs only when
the issue requires it, and update dependent connections. Retain FIXED SUBJECT DECISIONS, fixed
identity fields, valid additions and PROTECTED CONNECTIONS. Correct failed fields together and
recheck their endpoints. A failed unconfirmed proposal unsupported by CURRENT SOURCE may be
withdrawn together with its dependent connections. Confirmed decisions remain fixed. Use pending
for genuine unresolved identity and return the complete response.
"""


def examples():
    """Load independent contract examples; runtime imports contain no evaluator fixtures."""
    from pathlib import Path
    return json.loads(Path(__file__).with_name('subject_reference_examples.json').read_text())


def render(state):
    """Render the subject view directly, with evidence bases attached to their candidate."""
    from textwrap import indent
    import re
    from experiments.subject_recall import subject_stage_state, identity_excerpts
    from experiments.subject_validation import render_repair
    state=subject_stage_state(state)
    primary=state['new_user_source'];context=state.get('context_sources',[])
    lines=[]

    def section(title):
        if lines:lines.append('')
        lines.append('## '+title)

    def original(source,excerpts=None):
        content=source['content'] if excerpts is None else '\n\n'.join(excerpts)
        fence='`'*max(3,max((len(m.group())+1 for m in re.finditer(r'`+',content)),default=3))
        lines.extend([f"Source {source['id']} | speaker: {source['role']}",fence+'text',content,fence])

    section('CURRENT SOURCE');original(primary)
    from experiments.subject_references import current_evidence
    section('CURRENT EVIDENCE FRAGMENTS')
    for fragment in current_evidence(primary):
        lines.append(f"Evidence {fragment['id']} | source {fragment['source_id']}: {fragment['text']}")
    if context:
        section('SUPPORTING CONTEXT')
        for source in context:original(source)
    catalog=state.get('subject_catalog',[])
    if catalog:
        section('EXISTING SUBJECT CANDIDATES')
        if state.get('subject_recall',{}).get('truncated'):
            lines.append('The keyword candidate limit was reached; this is a partial candidate list.')
        for subject in catalog:
            lines.append(f"Subject {subject['id']} | type: {subject['kind']} | name: {subject['name']}")
            if subject.get('is_user'):lines.append('    Identity: the canonical conversation user (@user).')
            names=list(dict.fromkeys(a['name'] for a in subject.get('aliases',[]) if a['name']!=subject['name']))
            if names:lines.append('    Other known names: '+'; '.join(names))
            for description in subject.get('descriptions',[]):
                lines.append('    Source-backed description: '+description['text'])
            for basis in state.get('identity_bases',[]):
                if basis['target_id']!=subject['id']:continue
                origin=f"source {basis['source_id']}" if basis['source_id'] else 'API user identity'
                lines.append(f"    Basis {basis['id']} | {basis['kind']}: {basis['text']} | {origin}")
    if state.get('existing_connections'):
        section('EXISTING CONNECTIONS')
        for link in state['existing_connections']:lines.append(' -- '.join(link['endpoint_ids']))
    displayed={s['id']:s['content'] for s in [primary,*context]}
    additional=[source for sid,source in state.get('source_catalog',{}).items()
                if sid not in displayed or sid!=primary['id'] and displayed[sid]!=source['content']]
    if additional:
        section('IDENTITY ORIGINALS')
        for source in additional:original(source,identity_excerpts(source,state.get('identity_bases',[])))
    section('AVAILABLE SOURCE CITATIONS')
    lines.extend([', '.join(state['source_catalog']),
                  'Return the complete subjects and connections JSON for CURRENT SOURCE.'])
    if state.get('repair_errors'):lines.extend(['',render_repair(state)])
    if state.get('recovery'):
        lines.extend(['# PENDING CLAIM REVIEW',
            'Re-evaluate identities in CURRENT SOURCE using the later clarification in SUPPORTING CONTEXT.',
            'The original date still anchors its time words. Return identities for CURRENT SOURCE.',
            'Pending claim: '+state['recovery']['content']])
    return '\n'.join(lines)


def maintain(model, state, *, deadline):
    """Record exact messages and reuse the configured client's retry/timeout conventions."""
    from experiments.subject_recall import subject_stage_state
    staged=subject_stage_state(state)
    payload,restore=semantic_references(staged)
    # semantic_references translates source values; rebuild the lookup from translated IDs.
    payload['source_catalog']={source['id']:source for source in payload.get('source_catalog',{}).values()}
    repair=bool(payload.get('repair_errors'))
    prompt=PROMPT
    messages=[dict(role='system',content=prompt+('\n'+REPAIR_PROMPT if repair else ''))]
    for number,(example_input,output) in enumerate(examples(),1):
        messages.extend([dict(role='user',content=f'# INDEPENDENT EXAMPLE {number}\nLocal IDs and evidence are scoped to this example.\n\n'+render(example_input)),
            dict(role='assistant',content=json.dumps(output,ensure_ascii=False))])
    base={k:v for k,v in payload.items() if k not in {'previous_output','repair_errors','fixed_subjects','fixed_subject_fields','protected_connections'}}
    body='# MAINTENANCE INPUT\n\n'+render(base);messages.append(dict(role='user',content=body))
    if repair:
        from experiments.subject_validation import render_repair
        previous=json.dumps(payload['previous_output'],ensure_ascii=False)
        feedback=render_repair(payload)
        messages.extend([dict(role='assistant',content=previous),dict(role='user',content=feedback)])
        body+='\n'+previous+'\n'+feedback
    schema=scoped_schema(SubjectConnectionPlan,payload)
    source_ids=sorted(payload.get('source_catalog',{}))
    if source_ids:
        for definition in schema.get('$defs',{}).values():
            prop=definition.get('properties',{}).get('source_id')
            if prop is not None:prop['enum']=source_ids
    from experiments.subject_references import current_evidence
    properties=schema['$defs']['IdentityReference']['properties']
    properties['evidence_id']['enum']=[row['id'] for row in current_evidence(payload['new_user_source'])]
    bases=[row['id'] for row in payload.get('identity_bases',[])]
    properties['basis_id']=({'anyOf':[{'type':'string','enum':bases},{'type':'null'}]} if bases else {'type':'null'})
    for name in ['SubjectNameDraft','DescriptionDraft']:
        schema['$defs'][name]['properties']['source_id']['enum']=[payload['new_user_source']['id']]
    response_format=dict(type='json_schema',json_schema=dict(name='SubjectConnectionPlan',strict=True,schema=schema))
    request=dict(model=model.model,temperature=0,max_completion_tokens=6000,messages=messages,response_format=response_format)
    for attempt in range(2):
        start=monotonic();trace=dict(kind='add_subjects_repair' if state.get('repair_errors') else 'add_subjects',model=model.model,attempt=attempt+1,pending_recovery=bool(state.get('recovery')))
        try:
            response=model.client.chat.completions.create(**request,timeout=remaining(deadline,model.timeout))
            if response.usage:trace.update(input_tokens=response.usage.prompt_tokens,output_tokens=response.usage.completion_tokens)
            text=response.choices[0].message.content
            if not text or getattr(response.choices[0].message,'refusal',None):raise OutputInvalid('subject_response_unavailable')
            raw=json.loads(text)
            if not isinstance(raw,dict):raise OutputInvalid('subject_response_requires_object')
            trace.update(input_bytes=len(body.encode()),output_bytes=len(text.encode()))
            return restore(raw),request
        except (json.JSONDecodeError,OutputInvalid) as error:
            trace.update(error_label(error))
            raise OutputInvalid('subject_model_json_invalid') from error
        except Exception as error:
            trace.update(error_label(error))
            if attempt==0 and transient(error) and deadline-monotonic()>1:
                sleep(.2);continue
            raise ProviderUnavailable('subject_experiment_provider_unavailable') from error
        finally:
            trace['seconds']=monotonic()-start;model.calls.append(trace)


def review(raw, state, store):
    """One structured validation result drives audit projections and the repair request."""
    from experiments.subject_validation import issue, schema_issues, error_projection
    from remi.clock import parse_time
    from experiments.subject_recall import subject_stage_state
    current_id=state['new_user_source']['id']
    supplied_current=deepcopy(state.get('source_catalog',{}).get(current_id))
    shared = prepare_reference_state(state, store)
    state = subject_stage_state(shared); issues = []
    # Validate the incoming current catalog entry against the raw current source before projection.
    if supplied_current is not None:state['source_catalog'][current_id]=supplied_current
    if not isinstance(raw, dict):
        issues.append(issue('contract', [], 'subject_response_requires_object',
                            'Return one JSON object containing subjects and connections.'))
        raw = {}
    for field, limit in [('subjects', 96), ('connections', 48)]:
        if not isinstance(raw.get(field), list):
            issues.append(issue('contract', [field], field+'_require_list', 'Return this field as an array.',
                                expected={field: []}))
        elif len(raw[field]) > limit:
            issues.append(issue('contract', [field], field+'_exceed_limit', f'Return at most {limit} entries.'))
    for field in raw.keys() - {'subjects', 'connections'}:
        issues.append(issue('contract', [field], 'response_extra_field', 'Return the subjects and connections fields.'))
    safe = {key: value if isinstance(value, list) else [] for key, value in
            [('subjects', raw.get('subjects')), ('connections', raw.get('connections'))]}
    safe=deepcopy(safe)
    bindings, identity_issues = resolve_subjects(safe, state, store); issues.extend(identity_issues)
    declared = {s['ref'] for s in state['proposal']['subjects']}
    links = {}; pending = []; protected = []; primary = state['new_user_source']['id']
    clock = maintenance_clock(state)
    for index, row in enumerate(safe['connections'][:48]):
        prefix = ['connections', index]; start = len(issues)
        def fail(field, code, message, actual=None, expected=None, sources=()):
            issues.append(issue('connection', [*prefix, *field], code, message,
                                actual=actual, expected=expected, source_ids=sources))
        try: link = ConnectionProposal.model_validate(row)
        except ValidationError as error:
            issues.extend(schema_issues(error, prefix, scope='connection', returned=row)); continue
        refs = link.subject_refs
        if len(set(refs)) != 2:
            fail(['subject_refs'], 'connection_requires_two_distinct_refs', 'Use two distinct subject refs.', {'subject_refs': refs})
        if not set(refs) <= declared:
            fail(['subject_refs'], 'connection_requires_declared_subject_refs',
                 'Declare the participating identities in subjects and use their local refs.',
                 {'subject_refs': refs}, {'subject_refs': sorted(declared)})
        sids = list(dict.fromkeys(e.source_id for e in link.evidence))
        if state.get('recovery'):
            sids=list(dict.fromkeys([*sids,state['context_sources'][-1]['id']]))
        if primary not in sids:
            fail(['evidence'], 'connection_requires_current_source', 'Include CURRENT SOURCE as connection evidence.',
                 {'evidence': [e.model_dump() for e in link.evidence]}, {'source_id': primary}, [primary])
        for position, sid in enumerate(sids):
            source = state['source_catalog'].get(sid)
            if source is None or source.get('owner', state['owner']) != state['owner']:
                fail(['evidence', position, 'source_id'], 'connection_requires_owned_supplied_source',
                     'Choose a supplied original owned by this user.', {'source_id': sid})
            else:
                try:visible=parse_time(source.get('message_at') or source.get('created_at') or clock)<=parse_time(clock)
                except (ValueError,TypeError):visible=False
                if visible:continue
                fail(['evidence', position, 'source_id'], 'connection_requires_visible_source',
                     'Choose a source visible at the current source time.', {'source_id': sid})
        if len(issues) > start: continue
        ids = [bindings['subject_ids'].get(str(ref)) for ref in refs]
        if None in ids:
            protected.append(deepcopy(row))
            pending.append(dict(subject_refs=refs, source_ids=sids, reason='unresolved_subject')); continue
        if len(set(ids)) != 2:
            fail(['subject_refs'], 'connection_requires_distinct_subject_identities',
                 'Connect two distinct stored identities.', {'subject_refs': refs}); continue
        protected.append(deepcopy(row))
        pair = sorted(ids); key = stable_id('con_', state['owner'], *pair)
        previous = links.get(key) or store.get('edges', state['owner'], key) or {}
        links[key] = dict(id=key, owner=state['owner'], endpoint_ids=pair,
                         source_ids=list(dict.fromkeys([*previous.get('source_ids', []), *sids])))
    state.update(history=shared.get('history',[]), source_catalog=shared['source_catalog'])
    return dict(issues=issues,
                contract_errors=[i['code'] for i in issues if i['scope'] == 'contract'],
                subject_errors=error_projection(issues, 'identity'),
                alias_issues=state.get('subject_alias_issues', []), description_issues=state.get('subject_description_issues', []),
                connection_errors=error_projection(issues, 'connection'), bindings=bindings,
                connections=list(links.values()), protected_connections=protected, pending=pending, state=state)

def commit(reviewed,store):
    """Commit in-memory experiment records; unresolved/invalid links retain no physical edge."""
    state=reviewed['state'];owner=state['owner']
    for source in [state['new_user_source'],*state.get('context_sources',[])]:
        store.put('sources',owner,source['id'],source)
    for subject in reviewed['bindings']['entity_rows']:
        store.put('objects',owner,subject['id'],subject)
    for connection in reviewed['connections']:
        store.put('edges',owner,connection['id'],connection)


def experiment_state(state,store):
    """Fixture-only relocation of old relation-card statements into recalled factual evidence."""
    state=deepcopy(state)
    old=state.pop('relation_catalog',[])
    existing=[dict(endpoint_ids=row['endpoint_ids']) for row in store.list('edges',state['owner'])]
    for row in old:
        existing.append(dict(endpoint_ids=row['endpoint_ids']))
        if not any(card['id']==row['id'] for card in state.get('history',[])):
            state.setdefault('history',[]).append(dict(id=row['id'],record_type='fact',content=row['content'],
                entity_ids=row.get('ordered_subject_ids',row['endpoint_ids']),mode=row.get('mode','asserted'),sources=row.get('sources',[])))
    state['existing_connections']=existing
    return prepare_reference_state(state,store)


MEMORY_PROMPT=PROPOSE_PROMPT.replace(
    'The preceding stage handles identity aliases and continuing relationships between subjects. Extract distinct\n'
    'facts,preferences,lessons and events present alongside them. A source containing only naming or continuing\n'
    'relationships, or an acknowledgement without new useful content, can return items=[].',
    'The preceding stage handles subject identities and undirected navigation connections. Extract concrete\n'
    'relationship statements as facts, including work, ownership and family statements, with their original\n'
    'mode and attribution. Preserve preferences, lessons and particular events as their appropriate types.\n'
    'A naming-only statement or acknowledgement without new useful content can return items=[].')
