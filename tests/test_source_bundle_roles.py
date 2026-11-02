from remi.engine import MemoryEngine
from tests.test_search_roles import document


def test_source_package_retains_roles_of_each_contributing_memory():
    first = document('a').model_copy(update={'memory_id':'m1','source_ids':['shared']})
    second = document('b').model_copy(update={'memory_id':'m2','source_ids':['shared']})
    fused,roles = MemoryEngine._fuse_channels([
        {'channel':'base','role':'direct','weight':1,'hits':[(first,1)]},
        {'channel':'constraints','role':'constraint','weight':1,'hits':[(second,1)]},
    ], [first,second])
    assert len(fused) == 1
    assert roles[('memory',fused[0][0].memory_id)] == {'direct','constraint'}


def test_bundle_objectives_preserve_both_queries_without_leaking_to_other_sources():
    first = document('a').model_copy(update={'memory_id':'m1','source_ids':['shared']})
    second = document('b').model_copy(update={'memory_id':'m2','source_ids':['shared']})
    other = document('c').model_copy(update={'memory_id':'m3','source_ids':['elsewhere']})
    annotations = {('memory','m1'):{('direct','request details')},
                   ('memory','m2'):{('constraint','conditions affecting feasibility')},
                   ('memory','m3'):{('latest_state','unrelated state')}}
    result = MemoryEngine._bundle_annotations([(first,1),(other,1)], [first,second,other], annotations)
    assert result[('memory','m1')] == annotations[('memory','m1')] | annotations[('memory','m2')]
    assert result[('memory','m3')] == annotations[('memory','m3')]
    assert annotations[('memory','m1')] == {('direct','request details')}
