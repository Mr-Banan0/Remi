"""Structured, source-scoped validation feedback shared by subject review and repair."""
from copy import deepcopy


def issue(scope, path, code, message, *, ref=None, actual=None, expected=None, source_ids=()):
    return dict(scope=scope, path=list(path), code=code, message=message, ref=ref,
                actual=deepcopy(actual), expected=deepcopy(expected), source_ids=list(dict.fromkeys(source_ids)))


def schema_issues(error, prefix, *, scope='identity', ref=None, returned=None):
    result=[]
    for entry in error.errors(include_input=False, include_context=False, include_url=False):
        value=returned; present=True
        for part in entry['loc']:
            try:value=value[part]
            except (KeyError,IndexError,TypeError):present=False;break
        label=entry['loc'][-1] if entry['loc'] and isinstance(entry['loc'][-1],str) else 'value'
        actual={label:deepcopy(value)} if present else dict(present=False)
        result.append(issue(scope,[*prefix,*entry['loc']],'output_'+entry['type'],entry['msg'],ref=ref,
            actual=actual,expected=dict(rule='Return the field using the response schema.')))
    return result


def error_projection(issues, scope):
    """Compact audit fields are projections of the single structured validation result."""
    result = {}
    for entry in issues:
        if entry['scope'] == scope:
            key = entry['ref'] if scope == 'identity' else str(entry['path'][1])
            result.setdefault(key, entry['code'])
    return result


def render_repair(payload):
    """Render the program's observable constraints without deciding semantic identities."""
    import json
    lines = ['REPAIR TASK',
             'Correct the complete previous JSON response using the original task and evidence above.',
             'Keep each original integer ref attached to its original identity. Consolidate structurally duplicated refs when reported.',
             'Retain the fixed decisions and valid alias additions listed below.',
             'Withdraw an unconfirmed failed proposal when CURRENT SOURCE does not support it.',
             'Use pending for unresolved identity.',
             'Return the same subjects JSON contract.']
    for title, key in [('FIXED SUBJECT DECISIONS', 'fixed_subjects'),
                       ('FIXED IDENTITY FIELDS AND VALID ADDITIONS', 'fixed_subject_fields')]:
        lines.extend(['', title])
        rows = payload.get(key, [])
        lines.append(json.dumps(rows, ensure_ascii=False) if rows else 'None.')
    lines.extend(['', 'PROGRAM VALIDATION ERRORS'])
    for index, entry in enumerate(payload.get('repair_errors', []), 1):
        path = ''.join(f'[{part}]' if isinstance(part, int) else ('.' if i else '') + part
                       for i, part in enumerate(entry['path']))
        subject = f" | subject ref {entry['ref']}" if entry['ref'] is not None else ''
        lines.extend([f"Issue {index}{subject} | {path} | {entry['code']}",
                      '    Requirement: ' + entry['message']])
        if entry.get('actual') is not None:
            lines.append('    Returned: ' + json.dumps(entry['actual'], ensure_ascii=False))
        if entry.get('expected') is not None:
            lines.append('    Expected: ' + json.dumps(entry['expected'], ensure_ascii=False))
        if entry.get('source_ids'):
            lines.append('    Refer to original sources above: ' + ', '.join(entry['source_ids']))
    return '\n'.join(lines)
