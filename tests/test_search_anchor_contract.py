from remi.engine import MemoryEngine
from remi.models import Decision, PlannedSearch
from remi.providers import search_schema
from tests.helpers import TestEngine, ScriptedModel, request


def test_planner_schema_separates_document_and_source_ids():
    schema=search_schema(dict(evidence=[dict(id='document-A',source_ids=['source-A'])],
        current_conditions=[dict(id='memory-A',source_ids=['source-B'])]))
    anchor=schema['$defs']['PlannedSearch']['properties']['anchor_source_id']
    assert anchor=={'anyOf':[{'type':'string','enum':['source-A','source-B']},{'type':'null'}]}
    assert schema['properties']['relevant_memory_ids']['items']['enum']==['memory-A']
    empty=search_schema(dict(evidence=[],current_conditions=[]))
    assert empty['$defs']['PlannedSearch']['properties']['anchor_source_id']=={'type':'null'}
    assert empty['properties']['relevant_memory_ids']['maxItems']==0


def test_displayed_source_can_ground_a_date_absent_from_question():
    engine=TestEngine(model=ScriptedModel())
    engine.add(request('The kiln failed on 2023-08-09.',when='2023-08-10T10:00:00Z'))
    source=next(row for row in engine.store.list('sources','u1') if row['role']=='user')
    sources=engine.store.sources('u1',[source['id']])
    decision=Decision(searches=[PlannedSearch(role='temporal_anchor',query='kiln failure',
        time_expression='2023-08-09',anchor_source_id=source['id'])],relevant_memory_ids=[])
    steps,errors=MemoryEngine._compile_searches(decision,'What happened after the kiln failed?',
        '2026-01-01T00:00:00Z',sources)
    assert not errors and len(steps)==1
    decision.searches[0].time_expression='2023-08-11'
    steps,errors=MemoryEngine._compile_searches(decision,'What happened after the kiln failed?',
        '2026-01-01T00:00:00Z',sources)
    assert not steps and errors[0]['error']=='time_not_supported_by_anchor_source'


def test_unavailable_source_is_rejected_before_date_compilation():
    decision=Decision(searches=[PlannedSearch(role='temporal_anchor',query='kiln failure',
        anchor_source_id='document-A')],relevant_memory_ids=[])
    steps,errors=MemoryEngine._compile_searches(decision,'What happened?', '2026-01-01T00:00:00Z')
    assert not steps and errors[0]['error']=='anchor_source_not_available'
