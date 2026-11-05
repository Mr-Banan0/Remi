"""Request-local, content-free inclusive timings and explicit work counters.

Nested stage durations overlap; they are not additive CPU measurements.
Record counts mean returned records, not SQLite physical rows scanned.
"""
from contextvars import ContextVar
from functools import wraps
from time import perf_counter

_current = ContextVar('remi_performance', default=None)


def count(name, amount=1):
    metrics = _current.get()
    if metrics is not None:
        metrics['work'][name] = metrics['work'].get(name, 0) + amount


def measured(name, *, records=False):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            metrics = _current.get()
            if metrics is None:
                return function(*args, **kwargs)
            start = perf_counter()
            try:
                result = function(*args, **kwargs)
                if records:
                    count(name + '.returned', len(result) if result is not None else 0)
                return result
            finally:
                stage = metrics['stages'].setdefault(name, {'calls': 0, 'seconds': 0.0})
                stage['calls'] += 1
                stage['seconds'] += perf_counter() - start
        return wrapped
    return decorate


def request_performance(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        metrics = {'operation': function.__name__, 'stages': {}, 'work': {}, 'success': False}
        token = _current.set(metrics)
        start = perf_counter()
        observer = getattr(self, "runtime_metrics", None)
        if observer is not None:
            observer.begin(function.__name__)
        try:
            result = function(self, *args, **kwargs)
            metrics['success'] = True
            return result
        finally:
            metrics['total_seconds'] = perf_counter() - start
            self.last_performance = metrics
            _current.reset(token)
            if observer is not None:
                observer.finish(metrics)
    return wrapped
