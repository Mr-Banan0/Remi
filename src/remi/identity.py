"""Stable identities and bounded, source-clock alias candidates."""
import hashlib
import json
import re
import unicodedata


def stable_id(prefix, *parts):
    return prefix + hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def normalized_name(name):
    return ' '.join(unicodedata.normalize('NFKC', name).translate(str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"'})).casefold().split())



def source_name_present(name, text):
    # Letter/number boundaries distinguish Latin names inside longer words; CJK names use literal containment.
    left = r'(?<![a-z0-9_])' if name and name[0].isascii() and name[0].isalnum() else ''
    right = r'(?![a-z0-9_])' if name and name[-1].isascii() and name[-1].isalnum() else ''
    return bool(name and re.search(left + re.escape(name) + right, text))


def visible_entity(entity, as_of):
    from .clock import parse_time
    clock = parse_time(as_of)
    if parse_time(entity.observed_at) > clock:
        return None
    return entity.model_copy(update={'aliases': [a for a in entity.aliases if parse_time(a.observed_at) <= clock]})


def resolve_mentions(store, owner, mentions, sources, observed_at):
    """Bind exact source names and confirmed aliases inside one owner and knowledge time."""
    from .models import AliasEvidence, GraphObject
    from .clock import parse_time
    catalog = [v for raw in store.list('objects', owner)
               if (v := visible_entity(GraphObject.model_validate(raw), observed_at))]
    source_texts = [(s['id'], normalized_name(s['content']), s.get('message_at') or s['created_at']) for s in sources]
    resolved, objects, dispositions = [], {}, []
    for mention in mentions:
        name = mention.name.strip()
        norm = normalized_name(name)
        is_self = norm == 'self'
        support = next((s for s in source_texts if source_name_present(norm, s[1])), None)
        if is_self:
            support = source_texts[0]
        if support is None:
            dispositions.append({'name': name, 'status': 'pending', 'reason': 'mention_requires_source_name'})
            continue
        matches = [e for e in catalog if (e.is_self if is_self else not e.is_self and norm in
                   {normalized_name(e.name), *[normalized_name(a.name) for a in e.aliases]})]
        if len(matches) > 1:
            dispositions.append({'name': name, 'status': 'pending', 'reason': 'ambiguous_exact_identity'})
            continue
        key = matches[0].id if matches else stable_id('obj_', owner, 'self' if is_self else norm)
        raw = store.get('objects', owner, key)
        obj = GraphObject.model_validate(raw) if raw else GraphObject(id=key, owner=owner, name='self' if is_self else name,
              kind='person' if is_self else 'entity', is_self=is_self, observed_at=observed_at)
        if parse_time(obj.observed_at) > parse_time(observed_at):
            dispositions.append({'name': name, 'status': 'pending', 'reason': 'identity_not_yet_visible'})
            continue
        alias = AliasEvidence(name=name, source_id=support[0], observed_at=support[2])
        if not any(a.name == alias.name and a.source_id == alias.source_id for a in obj.aliases):
            obj.aliases.append(alias)
        objects[key] = obj
        if key not in resolved:
            resolved.append(key)
        if not matches:
            catalog.append(obj)
    return resolved, list(objects.values()), dispositions
