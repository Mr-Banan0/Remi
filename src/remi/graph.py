"""Program-derived relationship projections, selected by supporting memory revision."""
from .identity import stable_id
from .models import Edge
from .heat import visible_memory
from .clock import in_view


def project_edges(memory, revision, previous_id=None):
    records = []
    def add(origin, target, relation, label='', target_revision_id=None):
        records.append(Edge(id=stable_id('edge_', revision.id, origin, target, relation, label),
            owner=memory.owner, origin=origin, target=target, relation=relation, label=label,
            revision_id=revision.id, memory_id=memory.id, source_id=revision.source_ids[0], target_revision_id=target_revision_id))
    for source_id in revision.source_ids:
        add(revision.id, source_id, 'supported_by')
    if previous_id:
        add(revision.id, previous_id, 'revises')
    for role in revision.roles:
        add(memory.id, role.entity_ref, 'involves', role.role)
    for assertion in revision.assertions:
        add(assertion.subject_ref, assertion.object_ref, 'assertion', ('not ' if assertion.negated else '') + assertion.predicate)
    for link in revision.links:
        add(memory.id, link.target_ref, 'related', link.label, link.target_revision_id)
    return records


def active_edges(store, owner, as_of):
    active = {}
    for memory in store.memories(owner):
        view = visible_memory(memory, as_of)
        if view and in_view(view.current, as_of):
            active[view.id] = view.current.id
    return [edge for edge in store.edges_for(owner)
            if active.get(edge.memory_id) == edge.revision_id and edge.relation in {'involves', 'assertion', 'related'}]
