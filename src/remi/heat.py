"""Knowledge-time views and bounded background from source-reported statements."""
from datetime import timedelta
from .clock import in_view, parse_time


def visible_memory(memory, as_of):
    clock = parse_time(as_of)
    revisions = [r.model_copy(deep=True) for r in memory.revisions if parse_time(r.observed_at) <= clock]
    if not revisions:
        return None
    # Each record owns its source-clock history.
    view=memory.model_copy(update={'revisions': revisions})
    if memory.endpoint_ids and view.current.id!=memory.current.id:
        from .models import EvidenceRef
        view.connection_evidence=EvidenceRef(source_id=view.current.source_ids[0],quote=view.current.quote)
    return view


def hot_view(memories, as_of, limit=24, recent_days=30):
    clock, eligible = parse_time(as_of), []
    for original in memories:
        memory = visible_memory(original, as_of)
        if memory is None or not in_view(memory.current, as_of):
            continue
        current = memory.current
        if current.mode != 'asserted' or current.status in {'unknown', 'desired', 'cancelled', 'planned'}:
            continue
        if memory.record_type == 'event':
            continue
        confirmed = parse_time(current.observed_at)
        access = parse_time(memory.last_accessed_at) if memory.last_accessed_at else confirmed
        if access > clock:
            access = confirmed
        recent = max(confirmed, access) >= clock - timedelta(days=recent_days)
        priority = (memory.pinned, memory.impact == 'high', recent, confirmed, memory.id)
        eligible.append((priority, memory))
    return [memory for _, memory in sorted(eligible, key=lambda item: item[0], reverse=True)[:limit]]
