"""Bounded request-local derived views, keyed by storage change tokens."""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

from .performance import count

_current = ContextVar('remi_read_cache', default=None)


@contextmanager
def read_cache(max_bytes=32 * 1024 * 1024, max_entries=32):
    token = _current.set({'items': OrderedDict(), 'bytes': 0,
                          'max_bytes': max_bytes, 'max_entries': max_entries})
    try:
        yield
    finally:
        _current.reset(token)


def derived(key, build):
    cache = _current.get()
    if cache is not None and key in cache['items']:
        cache['items'].move_to_end(key)
        count('derived_cache_hits')
        return cache['items'][key][0]
    value, size = build()
    count('derived_cache_builds')
    if cache is not None and cache['max_entries'] > 0 and size <= cache['max_bytes']:
        while cache['items'] and (len(cache['items']) >= cache['max_entries']
                                  or cache['bytes'] + size > cache['max_bytes']):
            _, (_, removed) = cache['items'].popitem(last=False)
            cache['bytes'] -= removed
        cache['items'][key] = (value, size)
        cache['bytes'] += size
    return value
