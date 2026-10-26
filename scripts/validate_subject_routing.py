"""Evaluate deterministic routing and participant identity through complete experimental Add.

Fixtures and expected identities stay in this evaluator. All records use SubjectMemoryStore;
transport requests/replies are saved locally without credentials. --dry-run validates seeded
sources, graph, documents and recall/routing without constructing an API client.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]

from experiments.subject_engine import PIPELINE, SubjectGraphEngine, SubjectMemoryStore, shared_recall
from experiments.subject_identity import SubjectRecord
from experiments.subject_providers import SubjectTextModel
from remi.graph import project_edges
from remi.identity import normalized_name, source_name_present, stable_id
from remi.models import AddMessage, AddRequest, Memory, Revision, Source
from remi.settings import Settings, load_environment, private_key

PAST = '2028-04-10T10:00:00+00:00'
CURRENT = '2028-04-14T10:00:00+00:00'


def cases():
    """Eight general scenarios; the caller passes only ordinary records into the engine."""
    return [
        dict(id='zero_name_new_person', route='subject_first',
             text='My new colleague Mira helped me finish the invoice review today.',
             subjects=[], memories=[], expected=[], new_name='Mira',
             description_terms={'Mira': ['colleague']}),
        dict(id='single_alias_reuse', route='subject_first',
             text='Riz helped me review the revised payroll today.',
             subjects=[dict(key='risa', name='Risa', aliases=['Riz'], descriptions=['colleague'],
                            source='My colleague Risa is also called Riz.')],
             memories=[dict(key='payroll', text='Risa helped me check the payroll spreadsheet last week.',
                            subjects=['risa'], kind='event')], expected=['risa']),
        dict(id='two_distinct_names', route='subject_first',
             text='Omar and Tessa helped me test the emergency generator today.',
             subjects=[dict(key='omar', name='Omar', descriptions=['colleague'], source='Omar is my colleague.'),
                       dict(key='tessa', name='Tessa', descriptions=['colleague'], source='Tessa is my colleague.')],
             memories=[dict(key='generator', text='Omar and Tessa inspected the emergency generator with me last week.',
                            subjects=['omar', 'tessa'], kind='event')], expected=['omar', 'tessa']),
        dict(id='namesake_project_history', route='memory_first',
             text='Morgan sent me the revised installation plan for the rooftop sensors today.',
             subjects=[dict(key='sensor_morgan', name='Morgan', descriptions=['colleague'], source='Morgan is my colleague.'),
                       dict(key='gallery_morgan', name='Morgan', descriptions=['colleague'], source='Another colleague is also named Morgan.')],
             memories=[dict(key='rooftop', text='Morgan reviewed the rooftop sensor installation plan with me last week.',
                            subjects=['sensor_morgan'], kind='event'),
                       dict(key='gallery', text='The other Morgan helped me prepare the library art exhibition last week.',
                            subjects=['gallery_morgan'], kind='event')], expected=['sensor_morgan']),
        dict(id='namesake_person_pet', route='memory_first',
             text='My dog Vale needs me to take it to the vet for a checkup today.',
             subjects=[dict(key='cousin', name='Vale', descriptions=['cousin'], source='Vale is my cousin.'),
                       dict(key='dog', name='Vale', descriptions=['dog'], source='My dog is named Vale.')],
             memories=[dict(key='cousin_visit', text='My cousin Vale visited me for dinner last week.',
                            subjects=['cousin'], kind='event'),
                       dict(key='vet', text='I booked a vet checkup for my dog Vale.',
                            subjects=['dog'], kind='event')], expected=['dog']),
        dict(id='shared_alias_organizations', route='memory_first',
             text='HRN sent me the follow-up schedule for the flood-relief food deliveries today.',
             subjects=[dict(key='relief', name='Harbor Relief Network', kind='organization', aliases=['HRN'],
                            descriptions=['organization'],
                            source='Harbor Relief Network is an organization also known as HRN.'),
                       dict(key='robotics', name='Harbor Robotics Network', kind='organization', aliases=['HRN'],
                            descriptions=['organization'],
                            source='Harbor Robotics Network is a separate organization also known as HRN.')],
             memories=[dict(key='food', text='I coordinated flood-relief food deliveries with Harbor Relief Network.',
                            subjects=['relief'], kind='event'),
                       dict(key='robots', text='I discussed the warehouse robot prototype with Harbor Robotics Network.',
                            subjects=['robotics'], kind='event')], expected=['relief']),
        dict(id='third_namesake_new_person', route='memory_first',
             text='I met another person named Morgan today, for the first time. This is a new acquaintance.',
             subjects=[dict(key='sensor_morgan', name='Morgan', descriptions=['colleague'], source='Morgan is my colleague.'),
                       dict(key='gallery_morgan', name='Morgan', descriptions=['colleague'], source='Another colleague is also named Morgan.')],
             memories=[dict(key='rooftop', text='Morgan reviewed the rooftop sensor installation plan with me last week.',
                            subjects=['sensor_morgan'], kind='event'),
                       dict(key='gallery', text='The other Morgan helped me prepare the library art exhibition last week.',
                            subjects=['gallery_morgan'], kind='event')], expected=[], new_name='Morgan'),
        dict(id='namesake_insufficient_evidence', route='memory_first',
             text='Morgan sent me a message today.',
             subjects=[dict(key='sensor_morgan', name='Morgan', descriptions=['colleague'], source='Morgan is my colleague.'),
                       dict(key='gallery_morgan', name='Morgan', descriptions=['colleague'], source='Another colleague is also named Morgan.')],
             memories=[dict(key='rooftop', text='Morgan reviewed the rooftop sensor installation plan with me last week.',
                            subjects=['sensor_morgan'], kind='event'),
                       dict(key='gallery', text='The other Morgan helped me prepare the library art exhibition last week.',
                            subjects=['gallery_morgan'], kind='event')], expected=[], pending_name='Morgan'),
    ]


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def runtime_manifest():
    paths = sorted([*ROOT.glob('src/**/*.py'), *ROOT.glob('experiments/*.py'),
                    *ROOT.glob('experiments/*.json'), Path(__file__).resolve()])
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def original(owner, sid, text):
    return Source(id=sid, owner=owner, session_id='historical-evidence', request_id=sid, ordinal=0,
                  role='user', content=text, message_at=PAST, created_at=PAST)


def prepare_case(case, round_number):
    """Seed source-backed records and their actual searchable documents, with no model calls."""
    owner = f'routing:{case["id"]}:round-{round_number}'
    engine = SubjectGraphEngine(store=SubjectMemoryStore(), settings=Settings(add_timeout=240))
    user_id = stable_id('obj_', owner, 'user')
    user_source = original(owner, stable_id('src_', owner, 'seed-user'), 'I am the person using this assistant.')
    engine.store.put('sources', owner, user_source.id, user_source.model_dump())
    user = SubjectRecord(id=user_id, owner=owner, name='user', kind='individual', is_user=True,
                         aliases=[], descriptions=[], observed_at=PAST)
    engine.store.put('objects', owner, user.id, user.model_dump())
    identities = {'user': user_id}
    for spec in case['subjects']:
        oid = stable_id('obj_', owner, 'seed', spec['key'])
        identities[spec['key']] = oid
        source = original(owner, stable_id('src_', owner, 'seed-subject', spec['key']), spec['source'])
        engine.store.put('sources', owner, source.id, source.model_dump())
        names = list(dict.fromkeys([spec['name'], *spec.get('aliases', [])]))
        row = SubjectRecord(id=oid, owner=owner, name=spec['name'], kind=spec.get('kind', 'individual'),
            aliases=[dict(name=name, source_id=source.id, quote=source.content, observed_at=PAST) for name in names],
            descriptions=[dict(text=text, source_id=source.id, quote=source.content, observed_at=PAST)
                          for text in spec.get('descriptions', [])], observed_at=PAST)
        engine.store.put('objects', owner, oid, row.model_dump())
    memory_ids = {}
    for spec in case['memories']:
        sid = stable_id('src_', owner, 'seed-memory', spec['key'])
        source = original(owner, sid, spec['text'])
        engine.store.put('sources', owner, sid, source.model_dump())
        mid = stable_id('mem_', owner, 'seed', spec['key'])
        memory_ids[spec['key']] = mid
        entity_ids = [user_id, *[identities[key] for key in spec['subjects']]]
        revision = Revision(id=stable_id('rev_', mid, 'seed'), content=source.content,
            source_ids=[sid], quote=source.content, evidence=[dict(source_id=sid, quote=source.content)],
            entity_ids=entity_ids, user_involvement='involved', mode='asserted', context='source_reported',
            status='occurred' if spec['kind']=='event' else 'stated', planned_at=None, event_at=None,
            observed_at=PAST, created_at=PAST, time_resolution='unknown', time_precision='unknown', operation='create')
        memory = Memory(id=mid, owner=owner, record_type=spec['kind'], revisions=[revision])
        engine.store.put('memories', owner, mid, memory.model_dump())
        for edge in project_edges(memory, revision):
            engine.store.put('edges', owner, edge.id, edge.model_dump())
        for document in engine._documents([source], [memory]):
            engine.store.put('search_documents', owner, document.id, document.model_dump())
    timestamp = int(datetime.fromisoformat(CURRENT).timestamp()*1000)
    request = AddRequest(request_id='current-turn', user_id=owner, session_id='current-conversation',
                         messages=[AddMessage(role='user', content=case['text'], timestamp=timestamp)])
    current_sid = stable_id('src_', owner, request.request_id, 0)
    current = Source(id=current_sid, owner=owner, request_id=request.request_id, session_id=request.session_id,
                     ordinal=0, role='user', content=case['text'], message_at=CURRENT, created_at=CURRENT)
    recall, trace = shared_recall(engine, engine._add_state(current, [current], [current], monotonic()+30), monotonic()+30)
    oracle = dict(subject_ids=identities, memory_ids=memory_ids,
                  preset_ids=set(identities.values()), current_source_id=current_sid)
    return engine, request, recall, trace, oracle


def validate_fixture(case, engine, recall, oracle):
    """Check evaluator construction independently of the real model's behavior."""
    owner = recall['owner']
    for row in engine.store.list('objects', owner):
        SubjectRecord.model_validate(row)
        for field, label in [('aliases', 'name'), ('descriptions', 'text')]:
            for item in row[field]:
                source = engine.store.get('sources', owner, item['source_id'])
                if not source or not source_name_present(normalized_name(item[label]), normalized_name(source['content'])):
                    raise ValueError('fixture_identity_evidence_invalid')
    supplied = {row['id'] for row in recall['subject_catalog']}
    if not set(oracle['subject_ids'].values()) <= supplied:
        raise ValueError('fixture_candidates_missing')
    if recall['maintenance_route']['order'] != case['route']:
        raise ValueError('fixture_route_mismatch')
    if not set(oracle['memory_ids'].values()) <= {row['id'] for row in recall['history']}:
        raise ValueError('fixture_memories_missing')
    for memory in engine.store.memories(owner):
        Memory.model_validate(memory)
        if not any(document.memory_id == memory.id for document in engine.store.documents(owner)):
            raise ValueError('fixture_memory_document_missing')


def capture_transport(model, directory):
    directory.mkdir(parents=True, exist_ok=True)
    create = model.client.chat.completions.create
    attempts = []

    def capture(**request):
        started = monotonic()
        row = dict(started_at=datetime.now(timezone.utc).isoformat(),
            request={key:request[key] for key in ['model','temperature','max_completion_tokens',
                                                'messages','response_format','timeout'] if key in request})
        attempts.append(row)
        number = len(attempts)
        try:
            response = create(**request)
            choice = response.choices[0]
            row['response'] = dict(model=getattr(response, 'model', None), id=getattr(response, 'id', None),
                system_fingerprint=getattr(response, 'system_fingerprint', None), content=choice.message.content,
                refusal=getattr(choice.message, 'refusal', None), finish_reason=getattr(choice, 'finish_reason', None),
                usage=response.usage.model_dump() if response.usage else None)
            return response
        except Exception as error:
            row.update(error_type=type(error).__name__, http_status=getattr(error, 'status_code', None))
            raise
        finally:
            row['seconds'] = monotonic()-started
            write_json(directory/f'{number:03d}.json', row)

    model.client.chat.completions.create = capture
    return attempts


def contains_name(row, name):
    names = [row.get('name', ''), *[item['name'] if isinstance(item, dict) else item for item in row.get('aliases', [])]]
    wanted = normalized_name(name)
    return any(source_name_present(wanted, normalized_name(value)) for value in names)


def evaluate(case, engine, unit, oracle):
    identity = unit.get('identity', {})
    bindings = identity.get('bindings', {})
    accepted = set(bindings.get('subject_ids', {}).values())
    owner = next(iter(engine.store.records['objects'].values()))['owner']
    objects = engine.store.list('objects', owner)
    new_objects = [row for row in objects if row['id'] not in oracle['preset_ids']]
    current_memories = [memory for memory in engine.store.memories(owner)
                        if oracle['current_source_id'] in memory.current.source_ids]
    current_entity_ids = set(eid for memory in current_memories for eid in memory.current.entity_ids)
    user_id = oracle['subject_ids']['user']
    expected_ids = {oracle['subject_ids'][key] for key in case['expected']}
    checks = []
    def check(dimension, name, passed):
        checks.append(dict(dimension=dimension, name=name, passed=bool(passed)))

    route = unit.get('shared_recall', {}).get('maintenance_route', {}).get('order')
    check('route', 'deterministic_route', route == case['route'])
    stages = []
    for call in getattr(engine.model, 'calls', []):
        kind = call['kind']
        if kind.endswith('_repair'):
            continue
        if not stages or stages[-1] != kind:
            stages.append(kind)
    expected_stages = (['add_memories', 'add_subjects'] if case['route']=='memory_first'
                       else ['add_subjects', 'add_propose'])
    check('route', 'actual_model_stage_order', stages == expected_stages)
    if case.get('new_name'):
        matching = [row for row in new_objects if not row['is_user'] and contains_name(row, case['new_name'])]
        check('identity', 'one_distinct_new_subject', len(matching)==1 and len(new_objects)==1)
        if len(matching)==1:
            expected_ids.add(matching[0]['id'])
    else:
        check('identity', 'no_unrequested_new_subject', not new_objects)
    check('identity', 'expected_existing_subjects_selected', expected_ids <= accepted)
    check('identity', 'no_other_existing_subject_selected', accepted <= expected_ids | {user_id})
    check('canonical_user', 'current_first_person_is_canonical', user_id in accepted)
    check('canonical_user', 'one_canonical_user_record', len([row for row in objects if row['is_user']])==1
          and all(row['id']==user_id for row in objects if row['is_user']))

    if case.get('pending_name'):
        outputs = unit.get('outputs', {})
        raw = outputs.get('subjects_repair', outputs.get('subjects', {}))
        pending_refs = {row['ref'] for row in raw.get('subjects', [])
                        if row.get('operation')=='pending' and contains_name(row, case['pending_name'])}
        check('identity', 'ambiguous_referent_explicitly_pending', bool(pending_refs))
        check('memory_binding', 'unresolved_subject_has_no_committed_memory_binding',
              not (current_entity_ids - {user_id}))
        pending_ids = {row['candidate_id'] for row in unit.get('candidate_dispositions', []) if row['status']=='pending'}
        check('memory_binding', 'dependent_memory_remains_pending', any(
            item['id'] in pending_ids and pending_refs.intersection(item['subject_refs']) for item in unit.get('items', [])))
    else:
        check('memory_binding', 'current_memory_saved', bool(current_memories))
        check('memory_binding', 'all_expected_participants_reach_memory', expected_ids | {user_id} <= current_entity_ids)
        check('memory_binding', 'memory_has_no_wrong_identity', current_entity_ids <= expected_ids | {user_id})
    check('identity_protocol', 'final_identity_contract_valid', not identity.get('contract_errors') and not identity.get('subject_errors'))
    check('alias_protocol', 'final_alias_evidence_valid', not identity.get('alias_issues'))
    check('description_protocol', 'final_description_evidence_valid', not identity.get('description_issues'))
    check('connection_protocol', 'final_connection_contract_valid', not identity.get('connection_errors'))
    for name, terms in case.get('description_terms', {}).items():
        matching = [row for row in objects if row['id'] in expected_ids and contains_name(row, name)]
        for term in terms:
            check('description_coverage', f'{name}_description_{term}', any(
                source_name_present(normalized_name(term), normalized_name(item['text']))
                for row in matching for item in row.get('descriptions', [])))
    dimensions = {dimension:all(row['passed'] for row in checks if row['dimension']==dimension)
                  for dimension in {row['dimension'] for row in checks}}
    return dict(checks=checks, dimensions=dimensions,
        subject_passed=all(dimensions.get(key, False) for key in ['route','identity','canonical_user','identity_protocol']),
        passed=all(row['passed'] for row in checks), accepted_subject_ids=sorted(accepted),
        expected_current_subject_ids=sorted(expected_ids | {user_id}),
        new_subject_ids=[row['id'] for row in new_objects],
        current_memory_ids=[memory.id for memory in current_memories],
        current_memory_participant_ids=sorted(current_entity_ids))


def run_case(case, round_number, output, dry_run=False):
    directory = output/f'round-{round_number}'/case['id']
    directory.mkdir(parents=True, exist_ok=False)
    start = monotonic()
    result = dict(id=case['id'], round=round_number, model='gpt-4o-mini', pipeline=PIPELINE,
                  checks=[], dimensions={}, passed=False, subject_passed=False)
    engine = model = None
    attempts = []
    try:
        engine, request, recall, trace, oracle = prepare_case(case, round_number)
        validate_fixture(case, engine, recall, oracle)
        result.update(input=request.model_dump(), initial_recall=recall, initial_recall_trace=trace,
                      fixture_valid=True, fixture_subject_ids=oracle['subject_ids'],
                      fixture_memory_ids=oracle['memory_ids'])
        if dry_run:
            result.update(passed=True, subject_passed=None, dry_run=True,
                          dimensions=dict(route=True, fixture=True))
        else:
            key = private_key('LLM_API_KEY', 'LLM_API_KEY_FILE')
            if not key:
                raise ValueError('model_key_unavailable')
            model = SubjectTextModel('gpt-4o-mini', key,
                os.getenv('LLM_BASE_URL', 'https://api.quickrouter.us/v1'),
                timeout=float(os.getenv('REMI_LLM_TIMEOUT', '45')))
            attempts = capture_transport(model, directory/'transport')
            engine.model = model
            engine.add(request)
            progress = engine.store.get('requests', request.user_id, request.request_id)
            unit = progress['units']['0']
            result.update(evaluate(case, engine, unit, oracle), unit=deepcopy(unit),
                          request_status=progress['status'])
    except Exception as error:
        result.update(error_type=type(error).__name__, http_status=getattr(error, 'status_code', None))
        if engine is not None:
            progress = engine.store.get('requests', request.user_id, request.request_id)
            if progress:
                result['partial_request'] = deepcopy(progress)
    finally:
        result.update(seconds=monotonic()-start, calls=list(model.calls) if model is not None else [],
                      physical_attempts=len(attempts), databases_created=0, tables_created=0)
        if engine is not None:
            write_json(directory/'records.json', engine.store.snapshot())
            engine.close()
        write_json(directory/'result.json', result)
    print(json.dumps(dict(round=round_number, id=case['id'], passed=result['passed'],
        subject_passed=result['subject_passed'], failed_dimensions=[key for key,value in result['dimensions'].items() if not value],
        error_type=result.get('error_type')), ensure_ascii=False), flush=True)
    return result


def summarize(results, rounds, dry_run, wall_seconds):
    calls = [call for result in results for call in result.get('calls', [])]
    dimensions = sorted({name for result in results for name in result.get('dimensions', {})})
    return dict(model='gpt-4o-mini', pipeline=PIPELINE, rounds=rounds, cases=len(cases()), runs=len(results),
        dry_run=dry_run, passed_runs=sum(row['passed'] for row in results),
        subject_passed_runs=None if dry_run else sum(row['subject_passed'] is True for row in results),
        dimensions={name:dict(passed=sum(row.get('dimensions', {}).get(name) is True for row in results),
                             evaluated=sum(name in row.get('dimensions', {}) for row in results)) for name in dimensions},
        per_case={case['id']:dict(runs=sum(row['id']==case['id'] for row in results),
            passed=sum(row['passed'] for row in results if row['id']==case['id']),
            subject_passed=None if dry_run else sum(row['subject_passed'] is True for row in results if row['id']==case['id']),
            subject_stable_across_rounds=None if dry_run else all(row['subject_passed'] is True for row in results if row['id']==case['id']))
            for case in cases()},
        failures=[dict(id=row['id'], round=row['round'], error_type=row.get('error_type'),
                       failed_checks=[check for check in row['checks'] if not check['passed']])
                  for row in results if not row['passed']],
        normal_calls=sum(not call['kind'].endswith('_repair') for call in calls),
        repair_calls=sum(call['kind'].endswith('_repair') for call in calls),
        physical_attempts=sum(row['physical_attempts'] for row in results),
        input_tokens=sum(call.get('input_tokens', 0) for call in calls),
        output_tokens=sum(call.get('output_tokens', 0) for call in calls),
        api_seconds=sum(call.get('seconds', 0) for call in calls), wall_seconds=wall_seconds,
        provider_errors=dict(Counter(row['error_type'] for row in results if row.get('error_type'))),
        databases_created=0, tables_created=0,
        scope='Eight synthetic general identity/routing scenarios, repeated independently; all state is in MemoryStore.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.rounds < 1 or args.workers < 1:
        parser.error('rounds and workers must be positive')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.umask(0o077)
    before = runtime_manifest()
    write_json(output/'runtime-manifest.json', before)
    write_json(output/'inputs.json', cases())
    if not args.dry_run:
        load_environment(args.env_file.resolve())
    started = monotonic()
    work = [(deepcopy(case), number) for number in range(1, args.rounds+1) for case in cases()]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda item:run_case(*item, output, args.dry_run), work))
    after = runtime_manifest()
    summary = summarize(results, args.rounds, args.dry_run, monotonic()-started)
    summary.update(runtime_unchanged=before==after,
                   runtime_fingerprint=hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                   input_sha256=hashlib.sha256((output/'inputs.json').read_bytes()).hexdigest())
    write_json(output/'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if before != after:
        raise RuntimeError('runtime_changed_during_evaluation')


if __name__ == '__main__':
    main()
