"""Source and revision projections plus identity-based memory navigation."""
from .clock import in_view
from .heat import visible_memory
from .identity import stable_id
from .models import Edge


def project_edges(memory, revision, previous_id=None):
    records = []
    def add(origin, target, kind):
        records.append(Edge(id=stable_id('edge_', revision.id, origin, target, kind), owner=memory.owner,
            origin=origin, target=target, relation=kind, revision_id=revision.id, memory_id=memory.id,
            source_id=target if kind == 'supported_by' else revision.source_ids[0]))
    for source_id in revision.source_ids:
        add(revision.id, source_id, 'supported_by')
    if previous_id:
        add(revision.id, previous_id, 'revises')
    for entity_id in revision.entity_ids:
        add(memory.id, entity_id, 'mentions')
    return records


def active_edges(store, owner, as_of):
    active = {}
    for original in store.memories(owner):
        memory = visible_memory(original, as_of)
        if memory and in_view(memory.current, as_of):
            active[memory.id] = memory.current.id
    return [e for e in store.edges_for(owner) if e.relation == 'mentions' and active.get(e.memory_id) == e.revision_id]
