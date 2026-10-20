"""Knowledge-time views and bounded background from valid relation records."""
from datetime import timedelta
from .clock import in_view, parse_time


def visible_memory(memory, as_of):
    clock = parse_time(as_of)
    revisions = [r.model_copy(deep=True) for r in memory.revisions if parse_time(r.observed_at) <= clock]
    if not revisions:
        return None
    # Each record owns its source-clock history.
    return memory.model_copy(update={'revisions': revisions})


def hot_view(memories, as_of, limit=24, recent_days=30):
    clock, eligible = parse_time(as_of), []
    for original in memories:
        memory = visible_memory(original, as_of)
        if memory is None or not in_view(memory.current, as_of):
            continue
        current = memory.current
        if current.mode != 'asserted' or current.status in {'unknown', 'desired', 'cancelled', 'planned'}:
            continue
        if memory.record_type != 'relation':
            continue
        confirmed = parse_time(current.observed_at)
        access = parse_time(memory.last_accessed_at) if memory.last_accessed_at else confirmed
        if access > clock:
            access = confirmed
        recent = max(confirmed, access) >= clock - timedelta(days=recent_days)
        priority = (memory.pinned, memory.impact == 'high', memory.record_type == 'relation', recent, confirmed, memory.id)
        eligible.append((priority, memory))
    return [memory for _, memory in sorted(eligible, key=lambda item: item[0], reverse=True)[:limit]]
