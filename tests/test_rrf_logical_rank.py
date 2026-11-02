import pytest

from remi.engine import MemoryEngine
from tests.test_search_roles import document


def test_multiple_versions_take_one_rank_position_per_channel():
    versions = [document(f'a{i}').model_copy(update={
        'memory_id':'memory-a', 'source_ids':['source-a']}) for i in range(6)]
    other = document('b').model_copy(update={'memory_id':'memory-b', 'source_ids':['source-b']})
    hits = [(item, 1.0) for item in [*versions,other]]
    fused,_ = MemoryEngine._fuse_channels([
        {'channel':'base','role':'direct','weight':1.0,'hits':hits}], [*versions,other])
    scores = {item.memory_id:score for item,score in fused}
    assert scores['memory-a'] == pytest.approx(1/61)
    assert scores['memory-b'] == pytest.approx(1/62)


def test_duplicate_revision_count_cannot_break_equal_channel_rank_evidence():
    versions = [document(f'a{i}').model_copy(update={
        'memory_id':'memory-a','source_ids':['source-a']}) for i in range(6)]
    other = document('b').model_copy(update={'memory_id':'memory-b','source_ids':['source-b']})
    fused,_ = MemoryEngine._fuse_channels([
        {'channel':'base','role':'direct','weight':1.0,'hits':[(d,1.0) for d in [*versions,other]]},
        {'channel':'second','role':'direct','weight':1.0,'hits':[(other,1.0),(versions[0],1.0)]},
    ], [*versions,other])
    scores = {item.memory_id:score for item,score in fused}
    assert scores['memory-a'] == pytest.approx(scores['memory-b'])
