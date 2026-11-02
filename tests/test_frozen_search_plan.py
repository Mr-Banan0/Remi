import pytest

from remi.providers import ProviderUnavailable
from scripts.frozen_search_plan import FrozenSearchPlanner


def test_recorded_decisions_preserve_repair_order_and_do_not_mutate_trace():
    first={'searches':[]};second={'searches':[{'query':'specific stored query'}]}
    trace={'planner_attempts':[{'raw':first},{'raw':second}]}
    planner=FrozenSearchPlanner()
    planner.load(trace)
    planner.decide({},deadline=1)['searches'].append('mutation')
    assert first['searches']==[]
    assert planner.decide({},deadline=1)==second
    with pytest.raises(ProviderUnavailable,match='exhausted'):
        planner.decide({},deadline=1)
    assert planner.calls==[]


def test_missing_recorded_output_requires_complete_evidence():
    planner=FrozenSearchPlanner()
    with pytest.raises(ValueError,match='complete_recorded'):
        planner.load({'planner_attempts':[{'errors':['network failure']}]})
