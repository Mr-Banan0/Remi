"""Run independent attribution cases; expectations stay in the evaluator."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from time import monotonic

from remi.clock import parse_time
from remi.engine import MemoryEngine
from remi.identity import stable_id
from remi.models import AddMessage, AddRequest
from remi.providers import ProviderUnavailable, providers_from_environment
from remi.settings import Settings, load_environment
from remi.storage import SQLiteStore


def fingerprint():
    files = sorted([*Path('src').rglob('*.py'), *Path('scripts').glob('*.py')])
    manifest = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def validate_case(case, output):
    model, embedder, ranker = providers_from_environment()
    path = output / (case['id'] + '.sqlite3')
    if not path.exists(): SQLiteStore.initialize(path, allow_create_schema=True)
    engine = MemoryEngine(SQLiteStore(path), model, embedder, ranker, Settings(add_timeout=180))
    owner = case['id']; user_id = stable_id('obj_', owner, 'user')
    result = {'id': owner, 'steps': [], 'checks': [], 'database': str(path)}
    def check(name, passed): result['checks'].append({'name': name, 'passed': bool(passed)})
    try:
        for number, step in enumerate(case['steps']):
            at = int(parse_time('2027-04-02T10:00:00Z').timestamp() * 1000) + number * 3600000
            request = AddRequest(user_id=owner, session_id='independent-attribution', request_id=f'step-{number}',
                messages=[AddMessage(role='user', content=step['text'], timestamp=at)])
            start = monotonic(); failures = []
            for attempt in range(2):
                try:
                    engine.add(request)
                    break
                except Exception as error:
                    failures.append(type(error).__name__)
                    if not isinstance(error, ProviderUnavailable) or attempt == 1: break
            progress = engine.store.get('requests', owner, request.request_id)
            committed = progress and progress.get('status') == 'complete'
            check(f'step_{number}_committed', committed)
            units = (progress or {}).get('units', {})
            candidates = [c for u in units.values() for c in u.get('propose', {}).get('items', [])]
            pending_ids = {c['candidate_id'] for u in units.values() for c in u.get('candidate_dispositions', []) if c['status'] == 'pending'}
            pending = [c for c in candidates if c['id'] in pending_ids]
            memories = engine.store.memories(owner)
            current = [m for m in memories if any(s in (progress or {}).get('source_ids', []) for s in m.current.source_ids)]
            for i, expected in enumerate(step.get('claims', [])):
                # Only text and timestamps entered Add. These labels evaluate the committed output.
                terms = expected.get('contains_any') or [expected['contains']]
                matches = [m for m in current if any(t.casefold() in m.current.content.casefold() for t in terms)]
                held = [c for c in pending if any(t.casefold() in c['content'].casefold() for t in terms)]
                check(f'step_{number}_claim_{i}_coverage', bool(matches) or (expected.get('pending_allowed', False) and bool(held)))
                if matches:
                    check(f'step_{number}_claim_{i}_user_attribution', all(
                        (user_id in m.entity_ids) == expected['user'] and
                        m.current.user_involvement == ('involved' if expected['user'] else 'not_involved') for m in matches))
                    if expected.get('subject'):
                        check(f'step_{number}_claim_{i}_other_subject', all(any(
                            engine.store.get('objects', owner, eid)['name'].casefold() == expected['subject'].casefold()
                            for eid in m.entity_ids) for m in matches))
                if held:
                    check(f'step_{number}_claim_{i}_pending_has_no_confirmed_card', not matches)
            if step.get('empty'):
                check(f'step_{number}_empty_input_has_no_cards', not current and not candidates)
            check(f'step_{number}_binding_checks_pass', all(c['passed'] for u in units.values() for c in u.get('user_binding_checks', [])))
            result['steps'].append({'index': number, 'seconds': monotonic() - start, 'provider_failures': failures,
                'committed': bool(committed), 'created_cards': len(current), 'pending_candidates': len(pending),
                'reference_repairs': [c for u in units.values() for c in u.get('user_reference_checks', []) if c['action'] != 'consistent'],
                'review_changes': [c for u in units.values() for c in u.get('user_involvement_reviews', []) if c['changed']]})
            if not committed: break
        rows = {table: engine.store.list(table, owner) for table in ['sources', 'objects', 'memories', 'edges', 'requests']}
        users = [obj for obj in rows['objects'] if obj['is_user']]
        check('canonical_user_node_unique', len(users) <= 1 and all(o['id'] == user_id and o['name'] == 'user' for o in users))
        check('all_revision_user_links_match_projection', all(
            (r.user_involvement == 'involved') == (user_id in r.entity_ids) and
            set(r.entity_ids) == {e['target'] for e in rows['edges'] if e['relation'] == 'mentions' and e['revision_id'] == r.id}
            for m in engine.store.memories(owner) for r in m.revisions))
        check('all_source_data_retained', len(rows['sources']) == len(result['steps']))
        (output / (owner + '-records.json')).write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        result['provider_calls'] = {'text': list(model.calls), 'embedding': list(embedder.calls)}
        result['passed'] = len(result['steps']) == len(case['steps']) and all(c['passed'] for c in result['checks'])
    finally:
        engine.close()
    (output / (owner + '-result.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'id': owner, 'passed': result.get('passed', False),
        'failed': [c['name'] for c in result['checks'] if not c['passed']]}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(); load_environment(args.env_file)
    raw = args.cases.read_bytes(); cases = json.loads(raw)['cases']; frozen = fingerprint()
    manifest = {'code_sha256': frozen, 'input_sha256': hashlib.sha256(raw).hexdigest(),
        'selected_cases': [c['id'] for c in cases], 'tables': 6, 'expectations_enter_runtime': False}
    if args.resume:
        if json.loads((args.output / 'manifest.json').read_text()) != manifest:
            raise RuntimeError('resume_requires_same_code_and_cases')
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / 'inputs.json').write_bytes(raw)
        (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda c: validate_case(c, args.output), cases))
    report = {'code_sha256': frozen, 'code_unchanged_during_run': frozen == fingerprint(),
        'cases': len(results), 'passed_cases': sum(r['passed'] for r in results),
        'checks': sum(len(r['checks']) for r in results),
        'passed_checks': sum(c['passed'] for r in results for c in r['checks']),
        'created_cards': sum(s['created_cards'] for r in results for s in r['steps']),
        'pending_candidates': sum(s['pending_candidates'] for r in results for s in r['steps']),
        'reference_repairs': sum(len(s['reference_repairs']) for r in results for s in r['steps']),
        'review_changes': sum(len(s['review_changes']) for r in results for s in r['steps']),
        'mean_add_seconds': sum(s['seconds'] for r in results for s in r['steps']) / sum(len(r['steps']) for r in results),
        'text_calls': sum(len(r['provider_calls']['text']) for r in results),
        'repair_calls': sum('repair' in c['kind'] for r in results for c in r['provider_calls']['text'])}
    (args.output / 'summary.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__': main()
