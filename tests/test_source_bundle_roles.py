from remi.engine import MemoryEngine
from tests.test_search_roles import document


def test_shared_source_groups_memories_and_keeps_roles_independent():
    first = document('a').model_copy(update={'memory_id':'m1','source_ids':['shared']})
    second = document('b').model_copy(update={'memory_id':'m2','source_ids':['shared']})
    fused, roles = MemoryEngine._fuse_channels([
        {'channel':'base','role':'direct','weight':1,'hits':[(first,1)]},
        {'channel':'constraints','role':'constraint','weight':1,'hits':[(second,1)]},
    ], [first,second])
    assert len(fused) == 1
    assert roles == {('memory','m1'):{'direct'},('memory','m2'):{'constraint'}}


def test_same_memory_merges_roles_across_channels():
    first = document('a').model_copy(update={'memory_id':'m1'})
    fused, roles = MemoryEngine._fuse_channels([
        {'channel':'base','role':'direct','weight':1,'hits':[(first,1)]},
        {'channel':'constraints','role':'constraint','weight':1,'hits':[(first,1)]},
    ], [first])
    assert len(fused) == 1
    assert roles[('memory','m1')] == {'direct','constraint'}


def test_source_package_rrf_compacts_duplicate_positions():
    first = document('a').model_copy(update={'memory_id':'m1','source_ids':['shared']})
    second = document('b').model_copy(update={'memory_id':'m2','source_ids':['shared']})
    third = document('c').model_copy(update={'memory_id':'m3','source_ids':['other']})
    fused, _ = MemoryEngine._fuse_channels([
        {'role':'direct','hits':[(first,1),(second,.9),(third,.8)]}], [first,second,third])
    scores = {doc.source_ids[0]: score for doc,score in fused}
    assert scores == {'shared':1/61,'other':1/62}


def test_shared_secondary_source_keeps_separate_packages():
    first = document('a').model_copy(update={'memory_id':'m1','source_ids':['one','shared']})
    second = document('b').model_copy(update={'memory_id':'m2','source_ids':['two','shared']})
    fused, _ = MemoryEngine._fuse_channels([
        {'role':'direct','hits':[(first,1),(second,.9)]}], [first,second])
    assert len(fused) == 2
