"""Bounded source-clock hot view; access heat and factual validity stay separate."""
from __future__ import annotations

from datetime import timedelta

from .clock import in_view, parse_time


def visible_memory(memory, as_of):
    revisions = [revision for revision in memory.revisions if parse_time(revision.observed_at) <= parse_time(as_of)]
    return memory.model_copy(update={"revisions": revisions}) if revisions else None


def hot_view(memories, as_of, limit=24, recent_days=30, kinds=None):
    eligible = []
    clock = parse_time(as_of)
    for original in memories:
        memory = visible_memory(original, as_of)
        if memory is None or not in_view(memory.current, as_of):
            continue
        if kinds is not None and memory.kind not in kinds:
            continue
        current = memory.current
        if memory.kind != "event" and current.status == "planned":
            continue
        ongoing = memory.kind in {"preference", "fact"}
        pending = memory.kind == "event" and current.status == "planned"
        confirmed = parse_time(current.observed_at)
        access = parse_time(memory.last_accessed_at) if memory.last_accessed_at else confirmed
        # Future accesses from another replay clock cannot influence a historical query.
        if access > clock:
            access = confirmed
        recent = max(confirmed, access) >= clock - timedelta(days=recent_days)
        if memory.pinned or ongoing or pending or recent:
            priority = (memory.pinned, ongoing and memory.impact == "high", pending, ongoing, recent,
                        max(confirmed, access), memory.id)
            eligible.append((priority, memory))
    return [memory for _, memory in sorted(eligible, key=lambda item: item[0], reverse=True)[:limit]]
