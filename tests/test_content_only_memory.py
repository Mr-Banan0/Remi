"""Content-only writes preserve source clocks, revisions and legacy readability."""
from copy import deepcopy
from remi.models import Memory, SearchRequest
from remi.subject_extraction import ExtractionPlan, examples, prepare, render, schema
from remi.memory_maintenance import render_event, _wire_candidates
from remi.heat import visible_memory
from tests.test_memory_lifecycle import lifecycle, req, update, OWNER

RETIRED = {'status', 'planned_at', 'event_at', 'time_expression', 'time_evidence', 'time_precision', 'time_resolution'}


def test_live_schema_and_fewshots_express_dates_and_lifecycle_in_content():
    for state, answer in examples():
        ExtractionPlan.model_validate(answer)
        packet, _ = prepare(state)
        props = schema(packet)['$defs']['ExtractionItem']['properties']
        assert not RETIRED & props.keys()
        assert all(not RETIRED & item.keys() for item in answer['items'])


def test_cancel_revision_keeps_occurrence_date_in_prose_and_original_evidence():
    engine, model = lifecycle()
    before = engine.store.memories(OWNER)[0]
    model.writer = lambda state: [update(state, content='Aster cancelled the workshop originally planned for 2030-01-03.')]
    engine.add(req('Aster cancelled that workshop.', 'cancel-prose', 1))
    after = engine.store.memories(OWNER)[0]
    assert after.id == before.id and len(after.revisions) == 2
    assert 'cancelled' in after.current.content and '2030-01-03' in after.current.content
    assert len(after.current.source_ids) == 2
    assert not RETIRED & after.current.model_dump().keys()
    assert not RETIRED & after.current.maintenance_patch['values'].keys()
    assert visible_memory(after, '2030-01-01T23:59:59Z').current.content == before.current.content
    engine.model = None
    result = engine.search(SearchRequest(user_id=OWNER, query='workshop', top_k=10), '2030-01-10T00:00:00Z')
    assert 'cancelled' in result.data[0].content
    assert '"status"' not in result.data[0].content and '"planned_at"' not in result.data[0].content


def test_update_legacy_revision_clears_old_slots_only_in_new_version():
    engine, model = lifecycle()
    stored = engine.store.list('memories', OWNER)[0]
    stored['revisions'][0].update(status='planned', planned_at='2030-01-03T00:00:00+00:00', event_at=None)
    patch = stored['revisions'][0]['maintenance_patch']
    patch['fields'] += ['status', 'planned_at', 'event_at']
    patch['values'].update(status='planned', planned_at='2030-01-03T00:00:00+00:00', event_at=None, time_evidence=[])
    old = Memory.model_validate(stored)
    engine.store.put('memories', OWNER, old.id, old.model_dump())
    original = deepcopy(old.current.model_dump())
    model.writer = lambda state: [update(state, content='Aster cancelled the workshop originally planned for 2030-01-03.')]
    engine.add(req('Aster cancelled that workshop.', 'legacy-cancel', 1))
    current = engine.store.memories(OWNER)[0]
    assert current.revisions[0].model_dump() == original
    assert not RETIRED & current.current.model_dump().keys()
    assert Memory.model_validate(current.model_dump()).current.content == current.current.content
