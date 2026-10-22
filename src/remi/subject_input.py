"""Readable subject-stage evidence; program state keeps timestamps and retrieval metadata."""
from __future__ import annotations

from textwrap import indent


def render_subject_input(payload):
    """Use the same text protocol for live inputs, demonstrations and repair requests."""
    primary = payload['new_user_source']
    context = payload.get('context_sources', [])
    history = payload.get('history', [])
    relations = payload.get('relation_catalog', [])
    sources = {s['id']: s for s in [primary, *context]}
    for row in [*history, *relations]:
        for source in row.get('sources', []):
            sources.setdefault(source['id'], source)
    lines = []

    def section(title):
        lines.extend(['', title])

    def original(source):
        lines.append(f"Source {source['id']} | speaker: {source['role']}")
        lines.append(indent(source['content'], '    '))

    section('CURRENT SOURCE')
    original(primary)
    section('SUPPORTING CONTEXT')
    for source in context:
        original(source)
    if not context:
        lines.append('None.')

    section('EXISTING SUBJECT CANDIDATES')
    for subject in payload.get('subject_catalog', []):
        lines.append(f"Subject {subject['id']} | type: {subject['kind']} | name: {subject['name']}")
        if subject.get('is_user'):
            lines.append('    Identity: the canonical conversation user (@user).')
        aliases = subject.get('aliases', [])
        names = list(dict.fromkeys(a['name'] for a in aliases if a['name'] != subject['name']))
        if names:
            lines.append('    Other known names: ' + '; '.join(names))
        seen = set()
        for alias in aliases:
            quote = alias.get('quote', '')
            if not quote or quote in seen:
                continue
            seen.add(quote)
            sid = alias.get('source_id')
            if sid not in sources or quote not in sources[sid]['content']:
                sid = next((key for key, source in sources.items() if quote in source['content']), None)
            if sid is not None:
                lines.append(f'    Identity evidence: source {sid}.')
            else:
                lines.append('    Stored identity evidence (reference only):')
                lines.append(indent(quote, '        '))
    if not payload.get('subject_catalog'):
        lines.append('None.')

    section('EXISTING RELATIONSHIPS')
    for relation in relations:
        ordered = relation.get('ordered_subject_ids', relation.get('endpoint_ids', []))
        lines.append(f"Relationship {relation['id']} | ordered participants: {' -> '.join(ordered)} | expression: {relation.get('mode', 'asserted')}")
        lines.append('    Stored statement:')
        lines.append(indent(relation['content'], '        '))
        if relation.get('sources'):
            lines.append('    Evidence sources: ' + ', '.join(s['id'] for s in relation['sources']))
    if not relations:
        lines.append('None.')

    section('OTHER RECALLED MEMORIES')
    relation_ids = {r['id'] for r in relations}
    cards = [card for card in history if card['id'] not in relation_ids]
    for card in cards:
        lines.append(f"Memory {card['id']} | type: {card.get('record_type', 'unspecified')}")
        if card.get('entity_ids'):
            lines.append('    Participants: ' + ', '.join(card['entity_ids']))
        if card.get('mode'):
            lines.append('    Expression: ' + card['mode'])
        if card.get('status'):
            lines.append('    Status: ' + card['status'])
        if card.get('context'):
            lines.append('    Condition: ' + card['context'])
        lines.append('    Stored claim:')
        lines.append(indent(card.get('content', ''), '        '))
        if card.get('sources'):
            lines.append('    Evidence sources: ' + ', '.join(s['id'] for s in card['sources']))
    if not cards:
        lines.append('None.')

    earlier = {s['id'] for s in [primary, *context]}
    additional = [s for key, s in sources.items() if key not in earlier]
    if additional:
        section('RECALLED ORIGINAL SOURCES')
        for source in additional:
            original(source)

    def fields(value, prefix='    '):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (list, dict)):
                    lines.append(prefix + key + ':')
                    fields(item, prefix + '    ')
                else:
                    lines.append(prefix + key + ': ' + ('null' if item is None else str(item)))
        elif isinstance(value, list):
            if not value:
                lines.append(prefix + 'None.')
            for i, item in enumerate(value):
                lines.append(prefix + f'Item {i}:')
                fields(item, prefix + '    ')
        else:
            lines.append(prefix + str(value))

    for key, title in [('accepted_subjects', 'FIXED ACCEPTED SUBJECT DECISIONS'),
                       ('accepted_connections', 'FIXED ACCEPTED RELATIONSHIP DECISIONS'),
                       ('rejected_subjects', 'SUBJECT DECISIONS TO CORRECT'),
                       ('rejected_connections', 'RELATIONSHIP DECISIONS TO CORRECT'),
                       ('repair_errors', 'PROGRAM VALIDATION ERRORS')]:
        if payload.get(key):
            section(title)
            fields(payload[key])

    section('AVAILABLE SOURCE CITATIONS')
    lines.append(', '.join(sources))
    lines.append('Return the subjects and connections JSON response for CURRENT SOURCE.')
    return '\n'.join(lines).strip()
