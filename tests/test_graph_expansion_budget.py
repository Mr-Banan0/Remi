"""Graph expansion retains its own budget as direct retrieval grows."""
from remi.models import Edge
from remi.settings import Settings
from .helpers import TestEngine, ScriptedModel, request


def bank(count=26):
    engine=TestEngine(model=ScriptedModel(),settings=Settings(graph_limit=2))
    for index in range(count):
        engine.add(request(f'Independent archive item {index}.',request_id=f'r{index}'))
    memories=engine.store.memories('u1')
    docs=engine.store.documents('u1')
    by_memory={doc.memory_id:doc for doc in docs if doc.memory_id}
    return engine,memories,docs,by_memory


def connect(engine,left,right):
    left,right=sorted((left,right),key=lambda memory:memory.id)
    edge=Edge(id='edge-'+left.id+'-'+right.id,owner='u1',origin=left.id,target=right.id,relation='related',
        memory_id=left.id,revision_id=left.current.id,target_revision_id=right.current.id,
        source_id=left.current.source_ids[0],quote=left.current.quote)
    engine.store.put('edges','u1',edge.id,edge.model_dump())


def test_twenty_four_direct_seeds_leave_room_for_related_neighbor():
    engine,memories,docs,by_memory=bank()
    connect(engine,memories[0],memories[-1])
    hits=engine._graph_candidates('u1','unmatched query',
        [by_memory[m.id] for m in memories[:24]],docs,None,'2025-01-01T00:00:00Z')
    assert memories[-1].id in {doc.memory_id for doc,_ in hits}
    assert len(hits)<=2


def test_seed_budget_preserves_retrieval_order_instead_of_id_order():
    engine,memories,docs,by_memory=bank()
    ordered=sorted(memories,key=lambda m:m.id)
    first=ordered[-1];neighbor=ordered[-2]
    connect(engine,first,neighbor)
    seeds=[first,*ordered[:-2]]
    hits=engine._graph_candidates('u1','unmatched query',
        [by_memory[m.id] for m in seeds],docs,None,'2025-01-01T00:00:00Z')
    assert neighbor.id in {doc.memory_id for doc,_ in hits}


def test_neighbor_budget_uses_relevance_before_internal_id():
    engine,memories,docs,by_memory=bank(28)
    ordered=sorted(memories,key=lambda m:m.id)
    seed=ordered[0];target=ordered[-1]
    for neighbor in ordered[1:]:
        connect(engine,seed,neighbor)
    by_memory[target.id].text='Distinctive accessibility condition'
    hits=engine._graph_candidates('u1','Distinctive accessibility condition',
        [by_memory[seed.id]],docs,None,'2025-01-01T00:00:00Z')
    assert hits[0][0].memory_id==target.id
    assert len(hits)<=engine.settings.graph_limit
