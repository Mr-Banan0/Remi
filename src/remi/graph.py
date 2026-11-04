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
            source_id=target if kind == 'supported_by' else revision.source_ids[0] if kind=='related' else revision.source_ids[0]))
    for source_id in revision.source_ids:
        add(revision.id, source_id, 'supported_by')
    if previous_id:
        add(revision.id, previous_id, 'revises')
    if memory.endpoint_ids:
        add(*sorted(memory.endpoint_ids), 'related')
    for entity_id in revision.entity_ids:
        add(memory.id, entity_id, 'mentions')
    return records


def _active_versions(store, owner, as_of):
    return {memory.id: memory.current.id for original in store.memories(owner)
            if (memory := visible_memory(original, as_of)) and in_view(memory.current, as_of)}


def active_edges(store, owner, as_of, *, origins=None, targets=None, active=None):
    active = _active_versions(store, owner, as_of) if active is None else active
    return [edge for edge in store.edges_for(owner, origins=origins, targets=targets, relation='mentions')
            if active.get(edge.memory_id) == edge.revision_id]


def active_connections(store, owner, as_of, *, seeds=None, active=None):
    if active is None:
        active = {m.id: m.current.id for original in store.memories(owner)
                  if (m := visible_memory(original, as_of))}
    if seeds is None:
        edges = store.edges_for(owner, relation='related')
    else:
        keys = sorted(set(seeds))
        edges = {}
        # Bounded SQL parameter batches use the existing endpoint indexes.
        for offset in range(0, len(keys), 400):
            batch = keys[offset:offset + 400]
            for edge in (*store.edges_for(owner, origins=batch, relation='related'),
                         *store.edges_for(owner, targets=batch, relation='related')):
                edges[edge.id] = edge
        edges = list(edges.values())
    return [edge for edge in edges if active.get(edge.origin) == edge.revision_id
            and active.get(edge.target) == edge.target_revision_id]
