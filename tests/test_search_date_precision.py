from time import monotonic

import pytest

from remi.clock import yearless_calendar_day
from remi.engine import MemoryEngine
from remi.models import Decision, PlannedSearch, RecallStep
from tests.helpers import TestEngine
from tests.test_search_roles import document


@pytest.mark.parametrize('expression,text,expected', [
    ('April 3', 'What happened on April 3rd?', (4,3)),
    ('4月3日', '4月3日发生了什么？', (4,3)),
    ('2028-04-03', 'What happened on April 3?', (4,3)),
    ('April 3, 2024', 'What happened on April 3, 2024?', None),
    ('2024年4月3日', '2024年4月3日发生了什么？', None),
    ('yesterday', 'What happened yesterday?', None),
    ('February 29', 'What happened on February 29?', (2,29)),
    ('February 30', 'What happened on February 30?', None),
])
def test_query_calendar_precision(expression, text, expected):
    assert yearless_calendar_day(expression, text) == expected


def recall(expression, question, docs, relation='same_day', role='direct'):
    step = RecallStep(role=role, purpose='history', query='activity',
        time_expression=expression, anchor_source_id=None, time_basis='event',
        relation=relation, select='all', window_days=0)
    return TestEngine()._recall_step('u1', question, step, set(), docs,
        '2028-07-01T00:00:00Z', monotonic()+10)


def dated(name, event, observed=None):
    return document(name).model_copy(update=dict(text='activity', event_at=event,
        message_at=observed or event))


def test_yearless_calendar_search_keeps_all_matching_years():
    docs = [dated('older', '2022-04-03T00:00:00Z'), dated('recent', '2024-04-03T00:00:00Z'),
            dated('other', '2024-04-04T00:00:00Z')]
    hits, coverage = recall('April 3', 'What activity happened on April 3?', docs)
    assert {d.id for d,_ in hits} == {'older','recent'}
    assert coverage.endswith('year_unspecified')
    hits,_ = recall('April 3, 2024', 'What activity happened on April 3, 2024?', docs)
    assert [d.id for d,_ in hits] == ['recent']


def test_relative_day_uses_query_clock_and_partial_order_stays_unresolved():
    docs = [dated('yesterday', '2028-06-30T00:00:00Z'), dated('old', '2024-06-30T00:00:00Z')]
    hits,_ = recall('yesterday','What activity happened yesterday?',docs)
    assert [d.id for d,_ in hits] == ['yesterday']
    hits,coverage = recall('June 30','What happened after June 30?',docs,relation='after')
    assert not hits and coverage == 'time_scope_unresolved'


def test_premise_date_can_retrieve_source_observations_and_respects_known_event_time():
    docs = [dated('observed',None,'2024-04-03T00:00:00Z'),
            dated('different-event','2024-03-01T00:00:00Z','2024-04-03T00:00:00Z')]
    hits,_ = recall('April 3','What activity actually happened on April 3?',docs,role='premise_check')
    assert [d.id for d,_ in hits] == ['observed']


def test_yearless_leap_day_compiles_in_non_leap_query_year():
    decision = Decision(searches=[PlannedSearch(role='direct',query='activity',
        time_expression='February 29',relation='same_day')],relevant_memory_ids=[])
    steps,errors = MemoryEngine._compile_searches(decision, 'What activity happened on February 29?',
        '2027-01-01T00:00:00Z')
    assert not errors and steps[0].purpose == 'history'
