"""Logical packages contain the latest visible revision and actually hit revisions."""
from copy import deepcopy
from time import monotonic
import json
import httpx
import pytest
from remi.providers import JevReranker, RankResult
from remi.models import SearchRequest
from tests.test_memory_lifecycle import OWNER, lifecycle, req, update


def three_versions():
    engine, model = lifecycle()
    model.writer = lambda state: [update(state, content='Aster plans the workshop on January 5.')]
    engine.add(req('Aster moved that workshop to January 5.', 'move', 1))
    model.writer = lambda state: [update(state, content='Aster cancelled the workshop.')]
    engine.add(req('Aster cancelled the workshop.', 'cancel', 2))
    engine.model = None
    documents = engine._visible_documents(OWNER, '2030-01-10T00:00:00+00:00')
    versions = sorted([d for d in documents if d.memory_id], key=engine._revision_order)
    assert len(versions) == 3
    return engine, documents, versions


@pytest.mark.parametrize('indices,expected', [([2],[2]),([0],[2,0]),([0,1],[2,0,1]),([0,2],[2,0])])
def test_exact_version_union_and_latest_identity(indices, expected):
    engine, documents, versions = three_versions()
    before = [m.model_dump() for m in engine.store.memories(OWNER)]
    hits = [(versions[i], 100-i) for i in indices]
    expanded, _ = engine._expand_revision_chains(OWNER, hits, documents, '2030-01-10T00:00:00+00:00')
    packages = engine._merge(expanded, documents)
    assert len(packages) == 1
    package = packages[0][0]
    assert package.id == versions[2].id
    assert package.revision_id == versions[2].revision_id
    assert [e.revision_id for e in package.memory_evidence] == [versions[i].revision_id for i in expected]
    card = engine._card(OWNER, package, 'workshop', 6000, True)
    assert [v['revision_id'] for v in card['memory_package']['memories'][0]['versions']] == [versions[i].revision_id for i in expected]
    assert 'cancelled' in card['memory_package']['memories'][0]['versions'][0]['content']
    assert 'status' not in card['memory_package']['memories'][0]['versions'][0]
    assert sum(v['is_latest'] for v in card['memory_package']['memories'][0]['versions']) == 1
    assert [m.model_dump() for m in engine.store.memories(OWNER)] == before


def test_old_source_hit_does_not_activate_inherited_intermediate_versions():
    engine, documents, versions = three_versions()
    old_source = next(d for d in documents if not d.memory_id and versions[0].source_ids[0] in d.source_ids)
    hits, _ = engine._expand_revision_chains(OWNER, [(old_source, 1)], documents, '2030-01-10T00:00:00+00:00')
    package = engine._merge(hits, documents)[0][0]
    assert [e.revision_id for e in package.memory_evidence] == [versions[2].revision_id,versions[0].revision_id]


def test_historical_cutoff_uses_then_latest_and_never_future_cancel():
    engine, _, _ = three_versions()
    docs = engine._visible_documents(OWNER, '2030-01-02T23:00:00+00:00')
    versions = sorted([d for d in docs if d.memory_id], key=engine._revision_order)
    package = engine._merge([(versions[0], 1)], docs)[0][0]
    assert package.revision_id == versions[-1].revision_id
    card = engine._card(OWNER, package, 'workshop', 6000, True)
    assert len(card['memory_package']['memories'][0]['versions']) == 2
    assert 'cancelled' not in json.dumps(card)


def test_channels_union_hits_without_repeating_latest():
    engine, docs, versions = three_versions()
    merged, roles = engine._fuse_channels([
        dict(role='direct',weight=1,hits=[(versions[0],1)]),
        dict(role='latest_state',weight=1,hits=[(versions[1],1)])],docs)
    assert len(merged) == 1
    assert [e.revision_id for e in merged[0][0].memory_evidence] == [versions[i].revision_id for i in (2,0,1)]
    assert roles[('memory',versions[0].memory_id)] == {'direct','latest_state'}


def test_search_fallback_and_jev_share_latest_first_package():
    engine, docs, versions = three_versions()
    captured = []
    class Unavailable:
        def rank(self, query, cards, **kwargs):
            captured.extend(deepcopy(cards))
            return RankResult()
    engine.reranker = Unavailable()
    engine._recall_candidates = lambda *args, **kwargs: [(versions[0],1)]
    engine.settings = engine.settings.__class__(graph_enabled=False, hot_enabled=False)
    response = engine.search(SearchRequest(user_id=OWNER,query='workshop',top_k=10), '2030-01-10T00:00:00+00:00')
    assert len(response.data) == len(captured) == 1
    content = response.data[0].content
    package = captured[0]['memory_package']
    assert json.dumps(package, ensure_ascii=False) in content
    assert 'cancelled' in package['memories'][0]['versions'][0]['content']
    assert len(package['memories'][0]['versions']) == 2
    assert 'rerank_partial_or_unavailable' in engine.last_search['degradation']


def test_jev_receives_version_sources_and_package_scoring_rule():
    engine, docs, versions = three_versions()
    document = engine._merge([(versions[0],1)], docs)[0][0]
    card = engine._card(OWNER, document, 'workshop', 6000, True)
    def handler(request):
        body = json.loads(request.content)
        item = body['questions'][card['id']]['instructions']
        assert item['evidence']['memory_package'] == card['memory_package']
        assert 'latest visible revision' in item['question']
        assert item['evidence']['memory_package']['memories'][0]['versions'][0]['source_id'] in item['evidence']['original']
        return httpx.Response(200,json={'answers':{card['id']:{'type':'noul','noul':.9}}})
    ranker = JevReranker('test',transport=httpx.MockTransport(handler))
    try:
        assert ranker.rank('Is the workshop still planned?', [card], deadline=monotonic()+10).scores[card['id']] == .9
    finally:
        ranker.close()


def test_shared_source_retains_distinct_latest_states_and_references_once():
    engine, docs, versions = three_versions()
    current = versions[-1]
    second = current.model_copy(deep=True)
    second.id = 'second-document'
    second.memory_id = 'second-memory'
    second.revision_id = 'second-revision'
    second.memory_evidence = [current.memory_evidence[0].model_copy(update={
        'memory_id':second.memory_id, 'revision_id':second.revision_id,
        'content':'The dinner remains planned.', 'status':'planned'})]
    documents = [*docs, second]
    fused, _ = engine._fuse_channels([
        dict(role='direct',hits=[(versions[0],1)]),
        dict(role='constraint',hits=[(second,1)])],documents)
    assert len(fused) == 1
    card = engine._card(OWNER,fused[0][0],'workshop',6000,True)
    members = {m['memory_id']:m for m in card['memory_package']['memories']}
    assert 'cancelled' in members[current.memory_id]['versions'][0]['content']
    assert members[second.memory_id]['versions'][0]['status'] == 'planned'
    assert len(members[current.memory_id]['versions']) == 2
    assert len(members[second.memory_id]['versions']) == 1
    assert card['original'].count('['+current.source_ids[0]+']') == 1
    assert all('original' not in v for m in members.values() for v in m['versions'])
