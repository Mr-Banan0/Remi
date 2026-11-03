from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import math

from remi import retrieval


def reference(query, documents):
    counters = {d.id: Counter(retrieval.tokenize(d.text)) for d in documents}
    df = Counter(term for terms in counters.values() for term in terms)
    lengths = {key: sum(counter.values()) for key, counter in counters.items()}
    n = len(counters)
    average = sum(lengths.values()) / max(n, 1) or 1
    result = {}
    for key, terms in counters.items():
        score = 0.0
        for term in set(retrieval.tokenize(query)):
            tf = terms.get(term, 0)
            if tf:
                idf = math.log(1 + (n - df[term] + .5) / (df[term] + .5))
                score += idf * tf * 2.5 / (tf + 1.5 * (.25 + .75 * lengths[key] / average))
        if score:
            result[key] = score
    return result


def test_pool_changes_and_multilingual_scores_remain_exact():
    docs = [SimpleNamespace(id=str(i), text=text) for i, text in enumerate([
        '计划周五开会，改到周一。', 'meeting moved to Monday', '',
        'meeting meeting cancelled', 'Réunion lundi', '会議は月曜日です',
    ])]
    with retrieval.retrieval_cache():
        for pool in (docs, docs[:2], docs[1:], list(reversed(docs)), []):
            for query in ('会议周一', 'meeting Monday', 'Réunion', '月曜日', ''):
                assert retrieval.bm25(query, pool) == reference(query, pool)
        changed = [SimpleNamespace(id='0', text='new content Monday')]
        assert retrieval.bm25('Monday', changed) == reference('Monday', changed)


def test_repeated_pool_tokenized_once_and_exception_cleanup(monkeypatch):
    original = retrieval.tokenize
    calls = []
    def counted(text):
        calls.append(text)
        return original(text)
    monkeypatch.setattr(retrieval, 'tokenize', counted)
    docs = [SimpleNamespace(id='a', text='a unique historical document')]
    try:
        with retrieval.retrieval_cache():
            retrieval.bm25('first', docs)
            retrieval.bm25('second', docs)
            assert calls.count(docs[0].text) == 1
            raise RuntimeError('request failed')
    except RuntimeError:
        pass
    assert retrieval._lexical_cache.get() is None
    with retrieval.retrieval_cache():
        retrieval.bm25('third', docs)
    assert calls.count(docs[0].text) == 2


def test_cache_is_bounded_and_nested_requests_are_isolated():
    with retrieval.retrieval_cache():
        for i in range(4200):
            retrieval.term_counts('word ' + str(i))
        outer = retrieval._lexical_cache.get()
        assert len(outer['entries']) <= 4096
        retrieval.term_counts('x' * 1_000_001)
        assert outer['units'] <= 1_000_000
        with retrieval.retrieval_cache():
            assert retrieval._lexical_cache.get()['entries'] == {}
        assert retrieval._lexical_cache.get() is outer
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(retrieval._lexical_cache.get).result() is None
