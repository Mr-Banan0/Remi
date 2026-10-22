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


def subject_hits(store, owner, query, observed_at):
    from .models import GraphObject
    query = normalized_name(query)
    return [obj for raw in store.list('objects', owner)
        if (obj := visible_entity(GraphObject.model_validate(raw), observed_at))
        and any((source_name_present(normalized_name(n),query) or source_name_present(query,normalized_name(n))) for n in [*([] if obj.is_user else [obj.name]), *[a.name for a in obj.aliases]])]


def raw_subject_candidates(store, owner, sources, observed_at, limit=24):
    """Recall from original source words, preserving ambiguity and matched evidence."""
    from .models import GraphObject
    texts=[normalized_name(s['content']) for s in sources]
    ranked=[]
    for raw in store.list('objects',owner):
        obj=visible_entity(GraphObject.model_validate(raw),observed_at)
        if obj is None:continue
        names=list(dict.fromkeys([*([obj.name] if not obj.is_user else []),*[a.name for a in obj.aliases]]))
        matches=[name for name in names if any(source_name_present(normalized_name(name),text) for text in texts)]
        if not matches and not obj.is_user:continue
        # Current-source matches rank before context-only matches; all same-name hits compete equally.
        current=[name for name in matches if source_name_present(normalized_name(name),texts[0])]
        score=(bool(current),max((len(normalized_name(n)) for n in current or matches),default=0),obj.is_user)
        ranked.append((score,obj,matches))
    ranked.sort(key=lambda row:row[0],reverse=True)
    user=[(obj,matches) for _,obj,matches in ranked if obj.is_user]
    others=[(obj,matches) for _,obj,matches in ranked if not obj.is_user]
    return (user+others)[:limit]
