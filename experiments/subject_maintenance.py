"""Subject identity maintenance with names and three broad subject kinds.

This module uses MemoryStore only. The production Add pipeline remains V32.
"""
from __future__ import annotations

from copy import deepcopy
import json
from time import monotonic, sleep

from pydantic import Field

from remi.models import StrictModel
from experiments.subject_identity import SubjectDraft, resolve_subjects
from experiments.subject_references import prepare_reference_state
from remi.providers import semantic_references, scoped_schema, remaining, transient, error_label, ProviderUnavailable, OutputInvalid


class SubjectPlan(StrictModel):
    subjects: list[SubjectDraft] = Field(max_length=96)


PROMPT = """# Task
Resolve the identities in CURRENT SOURCE and return subjects as JSON.
subjects is the complete set of eligible identities referred to in CURRENT SOURCE. Reused rows keep unchanged stored fields.
Source text is attributed data. Each example is independent; the final MAINTENANCE INPUT is the live task.

EXISTING SUBJECT CANDIDATES are search results rather than a checklist. Return a candidate only when CURRENT
SOURCE refers to that identity through a name, alias, pronoun, possessive expression or other referring phrase.
SUPPORTING CONTEXT resolves a reference found in CURRENT SOURCE. Identities appearing only in SUPPORTING CONTEXT
stay outside the response.

# Scope
Subjects are particular people (person), animals (animal), and companies/institutions/teams (organization).
Equipment, places and projects are outside the subject output. An unnamed subject uses name=null until a real
name is stated. Roles, occupations, kinship terms, relationships, demographic attributes, social identities,
and specific animal categories such as dog or cat are memory content. Return them nowhere in name or aliases.
Descriptive noun phrases stay in evidence.
Use SUPPORTING CONTEXT to interpret current references. Each returned identity needs a current referent.

# Decide identities in this order
1. List the current eligible identities. Group names of the same identity under one ref.
2. Explicitly different referents are separate, including same-name people, animals or organizations. A role can
   distinguish two current referents, while aliases remains empty unless an alternative name is stated.
3. Explicit language that introduces another or different identity selects create even when its name matches a
   candidate. reuse a supplied E ID when context and its B basis establish the same identity. A word shared by multiple
   candidates is only a clue. create a concrete new referent with no supported match. pending means genuine ambiguity
   between multiple plausible identities or an unresolved referring expression. create/pending have target_id=null and reference.basis_id=null.
   A matching name or alias and compatible person/animal/organization evidence can resolve a candidate.
4. Return each identity once. For reuse, use that ID's kind and any of its known names or aliases.

# Operation decision table
- A current name or alias that uniquely matches one compatible candidate selects reuse, except when CURRENT SOURCE
  explicitly establishes a different identity.
- A current name matching multiple compatible candidates selects pending when CURRENT SOURCE and SUPPORTING CONTEXT
  provide no unique identity evidence.
- Explicit new-identity evidence, or a concrete referent with no matching candidate, selects create.
- Candidate identities and first-person expressions found only in SUPPORTING CONTEXT stay outside the response.

# Conversation user
The actual API user has name=@user,kind=person. Self-introductions and nicknames belong in that
same row's aliases. Reuse a candidate marked canonical user with its canonical_user B basis; otherwise
create @user. Return @user only when the actual user is a current participant expressed by first person, such as
I, me, my or 我. Authorship alone does not make the user a participant. Quoted first person belongs to its author.
E/B numbers are local labels with no role meaning.

# Names, aliases, kind and evidence
name is the first confirmed name stored for the subject. Keep that name on reuse. If a stored subject has
name=null and CURRENT SOURCE gives its first real name, return that name. aliases contains only new alternative
names, nicknames or abbreviations explicitly stated for the same subject in CURRENT SOURCE. Do not repeat the
canonical name or an existing alias. An alias must function as a proper name for this exact identity. Occupations,
roles, kinship terms, relationships, demographic attributes, animal categories and descriptive phrases are prohibited.
Examples that are not aliases include nurse, designer, colleague, sister, employee, man, dog, pet and the baker.
Keep unnamed referring phrases and pronouns in reference evidence rather than name or aliases.
Every subject has reference={evidence_id,basis_id}. Select a C fragment that supports this CURRENT
referent; the program copies its original wording and source. reuse selects an owned B basis as well;
create/pending select basis_id=null. C evidence establishes provenance; identity is your contextual
judgment. Existing candidates and historical originals help that judgment; returned subjects cover
current referents. Each alias addition cites CURRENT SOURCE through source_id.

# Response and check
Return subjects only. Each subject has ref,operation,target_id,name,kind,aliases,reference.
reference uses {evidence_id,basis_id}; select supplied C/B IDs. Aliases use {name,source_id}.
Operation gate: explicit evidence of another or different identity selects create; a uniquely established stored
identity selects reuse; two or more plausible same-kind stored identities without unique evidence select pending.
Before returning, check current identity coverage, exclude context-only candidates, distinguish explicit new
identities from unresolved collisions, preserve same-identity grouping, phrase ownership, and valid references.
Use empty addition arrays when unchanged.
"""


REPAIR_PROMPT = """# Repair task rules

Correct the previous complete subjects JSON using the field-level issues and original
evidence. For a current-evidence error, confirm that CURRENT SOURCE refers to that identity,
then select its supplied C fragment or withdraw the unconfirmed row. Keep each ref attached to its identity;
consolidate structurally duplicated refs only when the issue requires it. Retain FIXED SUBJECT DECISIONS,
fixed identity fields and valid additions. Correct failed fields together. A failed unconfirmed proposal
unsupported by CURRENT SOURCE may be withdrawn. Confirmed decisions remain fixed. Use pending
for genuine unresolved identity and return the complete response. When create overlaps a recalled
name or alias, select reuse or pending as appropriate. Repeat create only when CURRENT SOURCE
explicitly establishes a distinct new identity; the repeated decision confirms that evidence.
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
        section('SEARCH CANDIDATES — FILTER AGAINST CURRENT SOURCE')
        lines.append('These are possible identities found by current words or supporting context. Return only identities referred to in CURRENT SOURCE.')
        lines.append('Use SUPPORTING CONTEXT to resolve a current referring expression; omit identities that appear only in context.')
        if state.get('subject_recall',{}).get('truncated'):
            lines.append('The keyword candidate limit was reached; this is a partial candidate list.')
        for subject in catalog:
            name=subject.get('name') or '(unnamed)'
            lines.append(f"Subject {subject['id']} | type: {subject['kind']} | name: {name}")
            if subject.get('is_user'):lines.append('    Identity: the canonical conversation user (@user).')
            names=list(dict.fromkeys(a['name'] for a in subject.get('aliases',[]) if a['name']!=subject['name']))
            if names:lines.append('    Known aliases: '+'; '.join(names))
            for basis in state.get('identity_bases',[]):
                if basis['target_id']!=subject['id']:continue
                origin=f"source {basis['source_id']}" if basis['source_id'] else 'API user identity'
                label='alias' if basis['kind']=='name' else basis['kind']
                lines.append(f"    Basis {basis['id']} | {label}: {basis['text']} | {origin}")
    displayed={s['id']:s['content'] for s in [primary,*context]}
    additional=[source for sid,source in state.get('source_catalog',{}).items()
                if sid not in displayed or sid!=primary['id'] and displayed[sid]!=source['content']]
    if additional:
        section('IDENTITY ORIGINALS')
        for source in additional:original(source,identity_excerpts(source,state.get('identity_bases',[])))
    section('AVAILABLE SOURCE CITATIONS')
    lines.extend([', '.join(state['source_catalog']),
                  'Return the complete subjects JSON for CURRENT SOURCE.'])
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
    base={k:v for k,v in payload.items() if k not in {'previous_output','repair_errors','fixed_subjects','fixed_subject_fields'}}
    body='# MAINTENANCE INPUT\n\n'+render(base);messages.append(dict(role='user',content=body))
    if repair:
        from experiments.subject_validation import render_repair
        previous=json.dumps(payload['previous_output'],ensure_ascii=False)
        feedback=render_repair(payload)
        messages.extend([dict(role='assistant',content=previous),dict(role='user',content=feedback)])
        body+='\n'+previous+'\n'+feedback
    schema=scoped_schema(SubjectPlan,payload)
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
    schema['$defs']['SubjectNameDraft']['properties']['source_id']['enum']=[payload['new_user_source']['id']]
    response_format=dict(type='json_schema',json_schema=dict(name='SubjectPlan',strict=True,schema=schema))
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
    from experiments.subject_validation import issue, error_projection
    from experiments.subject_recall import subject_stage_state
    current_id=state['new_user_source']['id']
    supplied_current=deepcopy(state.get('source_catalog',{}).get(current_id))
    shared = prepare_reference_state(state, store)
    state = subject_stage_state(shared); issues = []
    # Validate the incoming current catalog entry against the raw current source before projection.
    if supplied_current is not None:state['source_catalog'][current_id]=supplied_current
    if not isinstance(raw, dict):
        issues.append(issue('contract', [], 'subject_response_requires_object',
                            'Return one JSON object containing subjects.'))
        raw = {}
    if not isinstance(raw.get('subjects'), list):
        issues.append(issue('contract', ['subjects'], 'subjects_require_list', 'Return subjects as an array.',
                            expected={'subjects': []}))
    elif len(raw['subjects']) > 96:
        issues.append(issue('contract', ['subjects'], 'subjects_exceed_limit', 'Return at most 96 entries.'))
    for field in raw.keys() - {'subjects'}:
        issues.append(issue('contract', [field], 'response_extra_field', 'Return the subjects field only.'))
    safe = {'subjects': deepcopy(raw.get('subjects')) if isinstance(raw.get('subjects'), list) else []}
    bindings, identity_issues = resolve_subjects(safe, state, store); issues.extend(identity_issues)
    state.update(history=shared.get('history',[]), source_catalog=shared['source_catalog'])
    return dict(issues=issues,
                contract_errors=[i['code'] for i in issues if i['scope'] == 'contract'],
                subject_errors=error_projection(issues, 'identity'),
                alias_issues=state.get('subject_alias_issues', []),
                bindings=bindings, state=state)

def commit(reviewed,store):
    """Commit source-backed subject identities."""
    state=reviewed['state'];owner=state['owner']
    for source in [state['new_user_source'],*state.get('context_sources',[])]:
        store.put('sources',owner,source['id'],source)
    for subject in reviewed['bindings']['entity_rows']:
        store.put('objects',owner,subject['id'],subject)
