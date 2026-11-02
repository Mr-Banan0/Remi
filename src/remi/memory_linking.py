"""Source-grounded, version-bound links between independent logical memories."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from time import monotonic, sleep

from pydantic import Field, ValidationError

from .graph import active_connections
from .heat import visible_memory
from .identity import stable_id
from .maintenance_clock import maintenance_clock
from .models import Edge, Memory, StrictModel
from .providers import OutputInvalid, ProviderUnavailable, error_label, remaining, strict_schema, transient
from .subject_references import current_evidence


class LinkChoice(StrictModel):
    pair_id: str = Field(pattern=r'^L[1-9][0-9]*$')
    reason: str = Field(min_length=1, max_length=240)
    related: bool


class LinkPlan(StrictModel):
    links: list[LinkChoice] = Field(max_length=48)


PROMPT = """# Task
Judge each supplied pair of memories. Return LinkPlan JSON using the supplied schema. For every pair, write one short concrete reason, then decide related=true or false.

# Meaning of a link
A related link is an internal search hint. Both memories remain separate. Select a pair when both describe the same identifiable activity, project or continuing situation and contribute different details, or when one supplies a specific applicable condition for the other. Contextual inference is allowed; explicit causation is optional.

Apply these rules to each pair:
1. Same identifiable project or activity: different concrete details such as scope, resources, location and schedule receive related=true.
2. Applicable condition: a current restriction, resource or preference that affects the activity receives related=true. Conflicting plans and constraints are especially useful together: related expresses relevance, not agreement or endorsement.
3. Explicit continuation between distinct memories receives related=true.
4. A shared person, emotion, broad subject or repeated routine alone receives related=false. Clearly different occurrences require an additional concrete connection. Ended conditions apply only to the matching historical circumstances.
Use the displayed participants and timing. Keep every memory's original meaning. Judge updated contents afresh. Cycles are allowed when each pair qualifies.

# Input
CURRENT MEMORIES are the finalized memories written by this turn. RECALLED MEMORIES are bounded cross-type candidates. ELIGIBLE PAIRS are the only selectable pairs. EVIDENCE contains exact source fragments.

# Output
Return links with one decision per supplied L handle. reason names the concrete shared situation or explains the missing connection. related=true means that retrieving one memory makes the other a useful contextual companion. A shared named project with complementary details qualifies. A currently applicable preference or restriction and an activity affected by it qualify even when the source does not explicitly connect them. Broad similarity alone receives related=false. The program records provenance and applies the expansion budget. Judge pairs independently.
"""


EXAMPLES = [
    (
        'PAIR L1\nA: Elin has a maximum rental budget of 700 euros per month.\nB: Elin is considering a flat costing 950 euros per month.',
        {'links': [{'pair_id': 'L1', 'reason': 'The flat exceeds Elin\'s applicable rental budget, so these details should be retrieved together.', 'related': True}]},
    ),
    (
        'PAIR L1\nA: The Cedar repair project needs replacement hinges.\nB: The Cedar repair project has a budget of 80 euros.\nE1: We set aside 80 euros for the Cedar repair project.\nAllowed evidence: E1',
        {'links': [{'pair_id': 'L1', 'reason': 'Complementary details of the Cedar repair project.', 'related': True}]},
    ),
    (
        'PAIR L1\nA: Last winter the bicycle had a flat tire.\nB: This autumn the bicycle needs new brake pads.\nE1: This autumn the bicycle needs new brake pads.\nAllowed evidence: E1',
        {'links': [{'pair_id': 'L1', 'reason': 'Separate bicycle repairs in different seasons.', 'related': False}]},
    ),
    (
        'PAIR L1\nA: The tenant currently lives in Bristol.\nB: The tenant completed a move to York last year.\nE1: The tenant has now moved from York to Bristol.\nAllowed evidence: E1\nReview the previously selected pair using the current contents.',
        {'links': [{'pair_id': 'L1', 'reason': 'The old York move does not describe current Bristol residence.', 'related': False}]},
    ),
    (
        """## CURRENT MEMORIES
N1 | event | Mira cancelled the hike.
N2 | fact | Mira's ankle injury currently limits walking.

## RECALLED MEMORIES
(none)

## ELIGIBLE PAIRS
L1 | N1 <-> N2

## EVIDENCE
E1 | Mira cancelled the hike because her ankle injury still limits walking.
""",
        {'links': [{'pair_id': 'L1', 'reason': 'Walking limitation explains hike cancellation.', 'related': True}]},
    ),
    (
        """## CURRENT MEMORIES
N1 | event | Mira's phone stopped working.
N2 | fact | Mira now uses a tablet for video calls.

## RECALLED MEMORIES
(none)

## ELIGIBLE PAIRS
L1 | N1 <-> N2 | evidence: E1, E2, E3

## EVIDENCE
E1 | Mira's phone stopped working.
E2 | Because of that failure, Mira now uses a tablet for video calls.
E3 | Mira's phone stopped working. Because of that failure, Mira now uses a tablet for video calls.
""",
        {'links': [{'pair_id': 'L1', 'reason': 'The tablet replaces the failed phone.', 'related': True}]},
    ),
    (
        """## CURRENT MEMORIES
N1 | preference | Mira likes jasmine tea.
N2 | fact | Mira's phone case is blue.

## RECALLED MEMORIES
(none)

## ELIGIBLE PAIRS
L1 | N1 <-> N2

## EVIDENCE
E1 | Mira likes jasmine tea. Her phone case is blue.
""",
        {'links': [{'pair_id': 'L1', 'reason': 'Tea preference and phone-case color only share a person.', 'related': False}]},
    ),
]


def _memory_row(handle, memory):
    return dict(handle=handle, memory_id=memory.id, revision_id=memory.current.id,
                record_type=memory.record_type, content=memory.current.content)


def build_state(store, state, memories, dispositions):
    """Build bounded current↔current and current↔recalled pair choices."""
    owner = state['owner']
    clock = maintenance_clock(state)
    touched = {memory.id: memory for memory in memories}
    by_candidate = {row['candidate_id']: row['memory_id'] for row in dispositions
                    if row.get('status') == 'accepted' and row.get('memory_id') in touched}
    candidate_order = [row['ref'] for row in state.get('memory_candidates', [])]
    current = []
    candidate_handles = {}
    for ref in candidate_order:
        memory_id = by_candidate.get(ref)
        if memory_id is None or memory_id in {row['memory_id'] for row in current}:
            continue
        handle = 'N' + str(len(current) + 1)
        current.append({**_memory_row(handle, touched[memory_id]), 'candidate_ref': ref})
        candidate_handles[ref] = handle

    recalled_by_id = {row['id']: deepcopy(row) for row in state.get('link_candidates', [])}
    existing_edges = active_connections(store, owner, clock)
    current_ids = {row['memory_id'] for row in current}
    preserved_raw=[]
    for edge in existing_edges:
        if edge.origin not in current_ids and edge.target not in current_ids:
            continue
        preserved_raw.append(edge)
        other = edge.target if edge.origin in current_ids else edge.origin
        if other in current_ids:
            continue
        raw = store.get('memories', owner, other)
        if raw is None:
            continue
        memory = visible_memory(Memory.model_validate(raw), clock)
        if memory is None:
            continue
        recalled_by_id.setdefault(other, dict(id=memory.id, revision_id=memory.current.id,
            record_type=memory.record_type, content=memory.current.content))

    recalled = []
    for memory_id, row in list(recalled_by_id.items())[:12]:
        if memory_id in current_ids:
            continue
        recalled.append(dict(handle='M' + str(len(recalled) + 1), memory_id=memory_id,
            revision_id=row['revision_id'], record_type=row['record_type'], content=row['content']))
    recalled_handles = {row['memory_id']: row['handle'] for row in recalled}
    handle_rows = {row['handle']: row for row in [*current, *recalled]}

    evidence = []
    fragments=current_evidence(state['new_user_source'])[:24]
    def add_evidence(source_id,text):
        if not text or any(row['source_id']==source_id and row['text']==text for row in evidence):
            return
        evidence.append(dict(id='E' + str(len(evidence) + 1),source_id=source_id,text=text))
    for fragment in fragments:
        add_evidence(fragment['source_id'],fragment['text'])
    source_text=state['new_user_source']['content']
    for left,right in zip(fragments,fragments[1:]):
        add_evidence(left['source_id'],source_text[left['start']:right['stop']])
    current_evidence_ids = [row['id'] for row in evidence
                            if row['source_id'] == state['new_user_source']['id']]

    candidates = []
    preserved_edges=[];preserved_pairs=set()
    by_memory_id={row['memory_id']:row for row in handle_rows.values()}
    for edge in preserved_raw:
        left,right=by_memory_id.get(edge.origin),by_memory_id.get(edge.target)
        if left is None or right is None:continue
        origin,target=sorted((left,right),key=lambda row:row['memory_id'])
        if (edge.revision_id != origin['revision_id'] or
                edge.target_revision_id != target['revision_id']):
            continue
        preserved_pairs.add(frozenset((origin['memory_id'],target['memory_id'])))
        preserved_edges.append(Edge(id=stable_id('edge_',origin['revision_id'],target['revision_id'],
            origin['memory_id'],target['memory_id'],'related'),owner=owner,
            origin=origin['memory_id'],target=target['memory_id'],relation='related',
            revision_id=origin['revision_id'],target_revision_id=target['revision_id'],
            memory_id=origin['memory_id'],source_id=edge.source_id,quote=edge.quote).model_dump())
    seen = set(preserved_pairs)
    truncated = False
    def add_pair(left_handle, right_handle):
        nonlocal truncated
        left, right = handle_rows[left_handle], handle_rows[right_handle]
        key = frozenset((left['memory_id'], right['memory_id']))
        if len(key) != 2 or key in seen:
            return
        if len(candidates) >= 48:
            truncated = True
            return
        seen.add(key)
        allowed = list(current_evidence_ids)
        if not allowed:
            return
        candidates.append(dict(id='L' + str(len(candidates) + 1), left=left_handle,
            right=right_handle, allowed_evidence_ids=allowed,existing=False,
            current_pair=left_handle.startswith('N') and right_handle.startswith('N')))

    for index, left in enumerate(current):
        for right in current[index + 1:]:
            add_pair(left['handle'], right['handle'])
    for edge in preserved_raw:
        left,right=by_memory_id.get(edge.origin),by_memory_id.get(edge.target)
        if left is not None and right is not None:
            add_pair(left['handle'],right['handle'])
    candidate_links = state.get('candidate_link_ids', {})
    for row in current:
        for memory_id in candidate_links.get(row['candidate_ref'], [])[:6]:
            if memory_id in recalled_handles:
                add_pair(row['handle'], recalled_handles[memory_id])
    background=[(edge.origin,edge.target) for edge in existing_edges
                if edge.origin not in current_ids and edge.target not in current_ids]
    return dict(owner=owner, current_memories=current, recalled_memories=recalled,
                pairs=candidates, evidence=evidence,
                source_provenance=dict(source_id=state['new_user_source']['id'],text=source_text),
                handles=handle_rows,background_links=background,preserved_edges=preserved_edges,
                truncated=truncated)


def render(state):
    lines = ['## CURRENT MEMORIES']
    lines.extend(f"{row['handle']} | {row['record_type']} | {row['content']}"
                 for row in state['current_memories'])
    lines.extend(['', '## RECALLED MEMORIES'])
    lines.extend((f"{row['handle']} | {row['record_type']} | {row['content']}"
                  for row in state['recalled_memories']),)
    if not state['recalled_memories']:
        lines.append('(none)')
    lines.extend(['', '## ELIGIBLE PAIRS'])
    for row in state['pairs']:
        left,right=state['handles'][row['left']],state['handles'][row['right']]
        lines.extend([f"{row['id']} | {row['left']} <-> {row['right']} | evidence: {', '.join(row['allowed_evidence_ids'])}",
                      'A: '+left['content'],'B: '+right['content']])
    lines.extend(['', '## EVIDENCE'])
    lines.extend(f"{row['id']} | {row['text']}" for row in state['evidence'])
    return '\n'.join(lines)


def provider_plan(model, state, *, deadline):
    schema = strict_schema(LinkPlan)
    choice = schema['$defs']['LinkChoice']['properties']
    choice['pair_id']['enum'] = [row['id'] for row in state['pairs']]
    body = render(state)
    messages = [dict(role='system', content=PROMPT)]
    for example, answer in EXAMPLES:
        messages.extend([dict(role='user', content=example),
                         dict(role='assistant', content=json.dumps(answer, ensure_ascii=False))])
    messages.append(dict(role='user', content=body))
    for attempt in range(2):
        started = monotonic()
        trace = dict(kind='add_links', model=model.model, attempt=attempt + 1)
        try:
            response = model.client.chat.completions.create(model=model.model, temperature=0,
                max_completion_tokens=min(6500, 500 + 120 * len(state['pairs'])),
                timeout=remaining(deadline, model.timeout), messages=messages,
                response_format=dict(type='json_schema', json_schema=dict(
                    name='LinkPlan', strict=True, schema=schema)))
            if response.usage:
                trace.update(input_tokens=response.usage.prompt_tokens,
                             output_tokens=response.usage.completion_tokens)
            message = response.choices[0].message
            if not message.content or getattr(message, 'refusal', None):
                raise OutputInvalid('link_response_unavailable')
            trace.update(input_bytes=len(body.encode()), output_bytes=len(message.content.encode()))
            result = json.loads(message.content)
            if not isinstance(result, dict):
                raise OutputInvalid('link_plan_requires_object')
            return result
        except (json.JSONDecodeError, ValidationError, OutputInvalid) as error:
            trace.update(error_label(error))
            raise OutputInvalid('link_model_output_invalid') from error
        except Exception as error:
            trace.update(error_label(error))
            if attempt == 0 and transient(error) and deadline - monotonic() > 1:
                sleep(.2)
                continue
            raise ProviderUnavailable('link_provider_unavailable') from error
        finally:
            trace['seconds'] = monotonic() - started
            model.calls.append(trace)
    raise ProviderUnavailable('link_provider_unavailable')


def validate_plan(raw, state):
    try:
        plan = LinkPlan.model_validate(raw)
    except ValidationError as error:
        return [], [dict(code='link_plan_schema_invalid', errors=error.errors(include_url=False))]
    pairs = {row['id']: row for row in state['pairs']}
    evidence = {row['id']: row for row in state['evidence']}
    handles = state['handles']
    counts = Counter(row.pair_id for row in plan.links)
    candidates, issues = [], []
    for choice in plan.links:
        pair = pairs.get(choice.pair_id)
        if pair is None or counts[choice.pair_id] != 1:
            issues.append(dict(code='link_pair_requires_unique_supplied_handle', pair_id=choice.pair_id))
            continue
        if not choice.related:
            continue
        candidates.append((pair,choice))
    for pair_id in pairs.keys() - counts.keys():
        issues.append(dict(code='link_pair_decision_missing',pair_id=pair_id))
    edges=[Edge.model_validate(row) for row in state.get('preserved_edges',[])]
    degrees=Counter()
    current_handles={row['memory_id']:row['handle'] for row in state['current_memories']}
    for edge in edges:
        degrees.update(current_handles[value] for value in (edge.origin,edge.target)
                       if value in current_handles)
    for pair,choice in candidates:
        left,right=handles[pair['left']],handles[pair['right']]
        current=[row['handle'] for row in (left,right) if row['handle'].startswith('N')]
        if any(degrees[handle]>=3 for handle in current):
            issues.append(dict(code='link_current_memory_budget',pair_id=pair['id']))
            continue
        degrees.update(current)
        origin,target=sorted((left,right),key=lambda row:row['memory_id'])
        selected=[evidence[value] for value in pair['allowed_evidence_ids'] if value in evidence]
        if not selected:
            issues.append(dict(code='link_source_missing',pair_id=pair['id']))
            continue
        provenance=state.get('source_provenance',selected[0])
        quote=provenance['text']
        edges.append(Edge(id=stable_id('edge_',origin['revision_id'],target['revision_id'],
            origin['memory_id'],target['memory_id'],'related'),owner=state['owner'],
            origin=origin['memory_id'],target=target['memory_id'],relation='related',
            revision_id=origin['revision_id'],target_revision_id=target['revision_id'],
            memory_id=origin['memory_id'],source_id=provenance['source_id'],quote=quote))
    return edges, issues

