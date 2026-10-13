"""BM25, multilingual tokenization and reciprocal-rank fusion; no task cue lists."""
from __future__ import annotations

import math
import re
from collections import Counter


def tokenize(text: str) -> list[str]:
    tokens = []
    for part in re.split(r"([\u3400-\u9fff]+)", text.casefold()):
        if not part:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]+", part) and len(part) > 1:
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
            tokens.extend(part[i:i + 3] for i in range(len(part) - 2))
        else:
            tokens.extend(re.findall(r"[\w]+", part, flags=re.UNICODE))
    return tokens


def bm25(query: str, documents) -> dict[str, float]:
    counters = {d.id: Counter(tokenize(d.text)) for d in documents}
    df = Counter(term for terms in counters.values() for term in terms)
    lengths = {key: sum(counter.values()) for key, counter in counters.items()}
    n = len(counters)
    average = sum(lengths.values()) / max(n, 1) or 1
    result = {}
    for key, terms in counters.items():
        score = 0.0
        for term in set(tokenize(query)):
            tf = terms.get(term, 0)
            if tf:
                idf = math.log(1 + (n - df[term] + .5) / (df[term] + .5))
                score += idf * tf * 2.5 / (tf + 1.5 * (.25 + .75 * lengths[key] / average))
        if score:
            result[key] = score
    return result


def validate_vector(vector, dimensions: int | None = None):
    if not isinstance(vector, list) or not vector or dimensions is not None and len(vector) != dimensions:
        raise ValueError("embedding_dimensions_invalid")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise ValueError("embedding_values_invalid")


def cosine(left, right) -> float:
    if len(left) != len(right):
        raise ValueError("embedding_dimensions_invalid")
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return numerator / denominator if denominator else 0.0


def hybrid(query, documents, vector=None, limit=40, semantic_scores=None):
    lexical = bm25(query, documents)
    lexical_ids = sorted(lexical, key=lambda key: (-lexical[key], key))
    semantic_ids = []
    if vector is not None:
        semantic = semantic_scores if semantic_scores is not None else {d.id: cosine(vector, d.vector) for d in documents if d.vector is not None}
        semantic_ids = sorted(semantic, key=lambda key: (-semantic[key], key))[:limit]
    scores = Counter()
    for ranking in (lexical_ids[:limit], semantic_ids):
        for position, key in enumerate(ranking, 1):
            scores[key] += 1 / (60 + position)
    by_id = {d.id: d for d in documents}
    return [(by_id[key], score) for key, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def clip_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")
