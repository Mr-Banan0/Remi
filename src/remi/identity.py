"""Stable identities and bounded, source-clock alias candidates."""
import hashlib
import json
import unicodedata


def stable_id(prefix, *parts):
    return prefix + hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def normalized_name(name):
    return ' '.join(unicodedata.normalize('NFKC', name).translate(str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"'})).casefold().split())


def entity_candidates(store, owner, mentions, as_of):
    from .clock import parse_time
    from .models import GraphObject
    names = {normalized_name(item.name) for item in mentions}
    result = []
    for raw in store.list('objects', owner):
        obj = GraphObject.model_validate(raw)
        if parse_time(obj.observed_at) > parse_time(as_of):
            continue
        aliases = [a.name for a in obj.aliases if parse_time(a.observed_at) <= parse_time(as_of)]
        matching = names & {normalized_name(n) for n in [obj.name, *aliases]}
        # Self is a bounded identity candidate, rather than a broadcast graph traversal seed.
        if matching or obj.is_self:
            result.append((len(matching), {**obj.model_dump(), 'aliases': aliases}))
    return [obj for _, obj in sorted(result, key=lambda item: (-item[0], item[1]['id']))[:12]]


def entity_kind(kind):
    kind = normalized_name(kind)
    groups = {
        'person': {'person', 'people', 'user', '人物', '人'},
        'place': {'place', 'location', 'city', '地点'},
        'organization': {'organization', 'organisation', 'company', 'org', 'client', '组织'},
        'project': {'project', '项目'},
        'document': {'document', 'file', '文档'},
        'thing': {'thing', 'object', 'device', 'resource', '物品'},
        'attribute': {'time', 'date', 'datetime', 'emotion', 'feeling', 'state', 'timestamp', '时间', '情绪', '状态'},
    }
    return next((name for name, values in groups.items() if kind in values), 'concept')
