"""Compact, transactionally maintained memory metadata for time-bound search."""
from collections.abc import Mapping
from types import SimpleNamespace
from .clock import parse_time
from .performance import count

PROJECTION_VERSION = 1


def project_memory(record):
    return {"version": PROJECTION_VERSION, "id": record["id"],
            "record_type": record["record_type"], "pinned": record.get("pinned", False),
            "impact": record.get("impact", "unknown"),
            "last_accessed_at": record.get("last_accessed_at"),
            "revisions": [{key: r.get(key) for key in
                ("id", "source_ids", "observed_at", "created_at", "mode", "status")}
                for r in record["revisions"]]}


class VisibleMemories(Mapping):
    """Metadata supports global navigation; complete histories load on selected IDs."""
    def __init__(self, store, owner, as_of):
        self.store, self.owner, self.as_of = store, owner, as_of
        self.headers = {}
        clock = parse_time(as_of)
        for row in store.memory_projections(owner):
            revisions = [SimpleNamespace(**r) for r in row["revisions"]
                         if parse_time(r["observed_at"]) <= clock]
            if revisions:
                current = max(revisions, key=lambda r: (parse_time(r.observed_at), parse_time(r.created_at), r.id))
                self.headers[row["id"]] = SimpleNamespace(**{
                    key: value for key, value in row.items() if key not in {"version", "revisions"}},
                    revisions=revisions, current=current)
        count("visible_memory_headers", len(self.headers))
        count("visible_revision_headers", sum(len(m.revisions) for m in self.headers.values()))

    def __iter__(self):
        return iter(self.headers)

    def __len__(self):
        return len(self.headers)

    def __contains__(self, key):
        return key in self.headers

    def __getitem__(self, key):
        if key not in self.headers:
            raise KeyError(key)
        from .heat import visible_memory
        memories = self.store.memories(self.owner, [key])
        count("visible_memory_bodies_loaded", len(memories))
        if not memories:
            raise KeyError(key)
        return visible_memory(memories[0], self.as_of)
