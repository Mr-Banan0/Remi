"""Small keyword identity candidates and source-backed identity evidence from one owned snapshot."""
from experiments.maintenance_clock import maintenance_clock
from copy import deepcopy
import re
import json

from remi.clock import parse_time
from remi.identity import normalized_name, source_name_present
from remi.providers import ProviderUnavailable
from remi.retrieval import clip_bytes
from experiments.subject_identity import CONTEXT_LIMIT, SUBJECT_LIMIT, keyword_score, matching_subjects
from experiments.subject_references import prepare_reference_state

RECALL_BYTES = 24000
HISTORY_LIMIT = 8
NAME_LIMIT = 12
IDENTITY_EXCERPT_BYTES = 800
MEMORY_SOURCE_LIMIT = 3
MEMORY_SOURCE_BYTES = 1600


def compact_subject(obj, texts):
    """Deduplicate presentation evidence; keep the full record in storage for maintenance."""
    row = obj.model_dump()
    for field, label, limit in [('aliases', 'name', NAME_LIMIT)]:
        def order(item):
            score = keyword_score(item[label], texts)
            return (-score[0], -score[1], -parse_time(item['observed_at']).timestamp(), item['source_id'])
        unique = {}
        for item in sorted(row[field], key=order):
            unique.setdefault(normalized_name(item[label]), {**item, 'quote': ''})
        row[field] = list(unique.values())[:limit]
    return row


def identity_excerpts(source, bases):
    """Render exact bounded source spans around each supplied basis, once per span."""
    text = source['content']
    if len(text.encode()) <= IDENTITY_EXCERPT_BYTES:
        return [text]
    spans = []
    for basis in bases:
        if basis.get('source_id') != source['id']:
            continue
        match = re.search(re.escape(basis['text']).replace(r'\ ', r'\s+'), text, flags=re.IGNORECASE)
        if match:
            # A complete literal basis fits even when it uses multibyte characters.
            start = max(0, match.start()-60)
            excerpt = clip_bytes(text[start:], max(IDENTITY_EXCERPT_BYTES, len(text[start:match.end()].encode())))
            if excerpt not in spans:
                spans.append(excerpt)
    return spans or [clip_bytes(text, IDENTITY_EXCERPT_BYTES)]


def subject_stage_state(state):
    """Present current words, the latest message and identity evidence from the shared snapshot."""
    state = deepcopy(state)
    state['context_sources'] = state.get('context_sources', [])[-CONTEXT_LIMIT:]
    state['history'] = []
    originals = {row['id']: row for row in [state['new_user_source'], *state['context_sources']]}
    for basis in state.get('identity_bases', []):
        sid = basis.get('source_id')
        if sid in state.get('source_catalog', {}):
            originals[sid] = state['source_catalog'][sid]
    state['source_catalog'] = originals
    return state


def recall_bytes(state):
    """Budget the rendered retrieval material, including identity bases and original excerpts.

    Current source, its latest supporting message and the fixed instructions have separate input sizes.
    """
    from experiments.subject_maintenance import render
    payload = {**state, 'new_user_source': {**state['new_user_source'], 'content': ''},
               'context_sources': [{**row, 'content': ''} for row in state.get('context_sources', [])]}
    return len(render(payload).encode()) + len(json.dumps(state.get('history', []), ensure_ascii=False).encode())


def maintenance_route(hits, current_text):
    """Choose order from current literal name/alias collisions, grouped by stored identity."""
    text = normalized_name(current_text)
    matches = {}
    for subject, _ in hits:
        labels = [*([subject.name] if subject.name and not subject.is_user else []), *[row.name for row in subject.aliases]]
        for label in labels:
            key = normalized_name(label)
            if key and source_name_present(key, text):
                group = matches.setdefault(key, dict(name=label, subject_ids=set()))
                group['subject_ids'].add(subject.id)
    groups = [dict(name=group['name'], subject_ids=sorted(group['subject_ids']))
              for _, group in sorted(matches.items())]
    collisions = [deepcopy(group) for group in groups if len(group['subject_ids']) >= 2]
    return dict(order='subject_first', name_matches=groups,
                collision_groups=collisions)


def prepare_candidates(store, state):
    """Fix at most six keyword candidates before allocating space to recalled memories."""
    state = deepcopy(state); owner = state['owner']; primary = state['new_user_source']
    clock = maintenance_clock(state)
    def visible(source):
        return source.get('owner', owner) == owner and parse_time(source.get('message_at') or source['created_at']) <= parse_time(clock)
    context = {row['id']: row for row in state.get('context_sources', [])
               if row['id'] != primary['id'] and row['role'] in {'user', 'assistant'} and visible(row)}
    state['context_sources'] = list(context.values())[-CONTEXT_LIMIT:]
    texts = [primary['content'], *[row['content'] for row in state['context_sources']]]
    hits = matching_subjects(store, owner, texts, clock, limit=None)
    route = maintenance_route(hits, primary['content'])
    users = [(obj, matches) for obj, matches in hits if obj.is_user]
    protected = {sid for group in route['collision_groups'] for sid in group['subject_ids']}
    protected.update(obj.id for obj, _ in users)
    if len(protected) > SUBJECT_LIMIT:
        raise ProviderUnavailable('subject_name_collision_exceeds_candidate_limit')
    # Preserve every collision alternative before spending remaining slots on other hits.
    selected_ids = set(protected)
    capacity = max(len(users) + SUBJECT_LIMIT - 1, len(protected))
    for obj, _ in hits:
        if len(selected_ids) >= capacity:
            break
        selected_ids.add(obj.id)
    selected = [(obj, matches) for obj, matches in hits if obj.id in selected_ids]
    catalog = [compact_subject(obj, [normalized_name(text) for text in texts]) for obj, _ in selected]
    state.update(subject_catalog=catalog, history=[], maintenance_route=route,
                 subject_recall=dict(candidate_limit=SUBJECT_LIMIT, matched_count=len(hits), truncated=len(hits)>len(selected)))

    def rebuild():
        return prepare_reference_state({**state, 'source_catalog': {row['id']: row for row in
            [primary, *state['context_sources']]}}, store)
    prepared = rebuild()
    # Spend optional display fields before affecting identity coverage. Every chosen ID stays present.
    while recall_bytes(prepared) > RECALL_BYTES:
        optional = [(row, 'aliases') for row in reversed(catalog) if len(row['aliases']) > 2]
        if not optional:
            raise ProviderUnavailable('subject_identity_evidence_exceeds_budget')
        row, field = optional[0]; row[field].pop(); prepared = rebuild()
    return prepared, dict(selected_subject_ids=[row['id'] for row in catalog],
        keyword_matched_subject_ids=[obj.id for obj, _ in hits],
        limit_skipped_subject_ids=[obj.id for obj, _ in hits if obj.id not in selected_ids], context_source_ids=[row['id'] for row in state['context_sources']],
        subject_candidate_limit=SUBJECT_LIMIT, subject_recall_truncated=len(hits)>len(selected))


def pack_history(store, state, cards):
    """Pack current memory views with bounded evidence; full revisions remain in storage."""
    selected = []; skipped = []; compacted = []; prepared = state; owner = state['owner']
    primary = state['new_user_source']; clock = maintenance_clock(state)
    seen = set()
    for card in cards:
        if len(selected) == state.get('history_limit', HISTORY_LIMIT):
            break
        if card['id'] in seen:
            continue
        seen.add(card['id'])
        card=deepcopy(card); originals=card.get('sources', [])
        if any(row.get('owner', owner) != owner or parse_time(row.get('message_at') or row['created_at']) > parse_time(clock)
               for row in originals):
            skipped.append(card['id']); continue
        bounded=[]
        for row in originals[:MEMORY_SOURCE_LIMIT]:
            value=deepcopy(row);content=value.get('content','')
            if len(content.encode())>MEMORY_SOURCE_BYTES:
                value['content']=clip_bytes(content,MEMORY_SOURCE_BYTES);compacted.append(card['id'])
            bounded.append(value)
        if len(originals)>len(bounded):compacted.append(card['id'])
        card['sources']=bounded
        sources = dict(prepared['source_catalog']); sources.update({row['id']: row for row in bounded})
        trial = {**state, 'history': [*selected, card], 'source_catalog': sources}
        # Evidence is optional presentation material; preserve the current view before dropping a card.
        while recall_bytes(trial)>RECALL_BYTES and card['sources']:
            removed=card['sources'].pop();sources.pop(removed['id'],None);compacted.append(card['id'])
            trial={**state,'history':[*selected,card],'source_catalog':sources}
        if recall_bytes(trial) > RECALL_BYTES:
            skipped.append(card['id']); continue
        selected.append(card); prepared = trial
    return prepared, dict(selected_memory_ids=[row['id'] for row in selected], budget_skipped_memory_ids=skipped,
                          compacted_memory_ids=sorted(set(compacted)),recall_bytes=recall_bytes(prepared), recall_byte_limit=RECALL_BYTES)
