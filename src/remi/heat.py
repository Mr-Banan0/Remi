"""Source-clock revision views and bounded, scope-aware current background."""
from datetime import timedelta
from .clock import in_view, parse_time


def visible_memory(memory, as_of):
    clock = parse_time(as_of)
    revisions = [r.model_copy(deep=True) for r in memory.revisions if parse_time(r.observed_at) <= clock]
    if not revisions:
        return None
    # Later known versions close earlier state intervals in the view; stored evidence remains immutable.
    by_id = {r.id: r for r in revisions}
    for revision in revisions:
        previous = by_id.get(revision.replaces_revision_id)
        if previous and previous.temporal_scope == 'ongoing' and revision.valid_from and revision.change in {'replace', 'withdraw'}:
            boundary = revision.valid_from
            if previous.valid_from and parse_time(boundary) > parse_time(previous.valid_from):
                previous.valid_to = boundary
    eligible = [r for r in revisions if not (r.temporal_scope == 'ongoing' and r.valid_from and parse_time(r.valid_from) > clock)]
    return memory.model_copy(update={'revisions': eligible or revisions})


def hot_view(memories, as_of, limit=24, recent_days=30, kinds=None):
    clock = parse_time(as_of)
    eligible = []
    for original in memories:
        memory = visible_memory(original, as_of)
        if memory is None or not in_view(memory.current, as_of):
            continue
        current = memory.current
        if current.status in {'desired', 'unknown', 'retracted', 'cancelled'}:
            continue
        if kinds is not None and current.status == 'planned':
            continue
        if kinds is not None and not set(memory.tags).intersection(kinds):
            continue
        ongoing = current.temporal_scope == 'ongoing'
        pending = current.status == 'planned'
        confirmed = parse_time(current.observed_at)
        access = parse_time(memory.last_accessed_at) if memory.last_accessed_at else confirmed
        if access > clock:
            access = confirmed
        recent = max(confirmed, access) >= clock - timedelta(days=recent_days)
        # Situational states describe the reported date; unknown persistence is shown as dated history.
        in_scope = ongoing or pending or confirmed.date() == clock.date()
        if in_scope and (memory.pinned or ongoing or pending or recent):
            priority = (memory.pinned, ongoing and memory.impact == 'high', pending, ongoing, recent, confirmed, memory.id)
            eligible.append((priority, memory))
    return [memory for _, memory in sorted(eligible, key=lambda x: x[0], reverse=True)[:limit]]
