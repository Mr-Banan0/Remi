"""Shared visible-original scoring across Remi and frozen TraceWeave outputs."""
from __future__ import annotations

import re
from collections import defaultdict

from remi.engine import stable_id


REMI_BLOCK = re.compile(
    r"\[(src_[a-f0-9]{24})\] (user|assistant|system) at [^\n]*\n(.*?)"
    r"(?=\n\n\[src_[a-f0-9]{24}\]|\nMemory:|\nMemory revisions:|\nRetrieval coverage:|\Z)", re.S)
TRACE_BLOCK = re.compile(
    r"\[source:([a-f0-9]{24})\]\n原始消息（(user|assistant|system)）[^\n]*\n(.*?)"
    r"(?=\n{1,2}(?:\[source:|\[retrieval\]|事件：|事实（|规则 version=)|\Z)", re.S)


def normalize(text):
    return " ".join(text.split())


def input_catalog(payload, turns):
    mapping, catalog = {}, {}
    for index, message in enumerate(payload["messages"]):
        key = stable_id("src_", payload["user_id"], payload["request_id"], index)
        mapping[key] = turns[index // 2]["ref"]
        catalog[key] = {"role": message["role"], "content": message["content"]}
    return mapping, catalog


def visible_blocks(candidates, mapping, catalog, framework="remi"):
    if framework not in {"remi", "embergraph", "traceweave"}:
        raise ValueError("supported_evidence_format_required")
    own_format = framework in {"remi", "embergraph"}
    for rank, candidate in enumerate(candidates, 1):
        content = candidate.get("content", "")
        if own_format:
            if "Original evidence:\n" not in content:
                continue
            content = content.split("Original evidence:\n", 1)[1]
            blocks = REMI_BLOCK.findall(content)
        else:
            blocks = TRACE_BLOCK.findall(content)
        for key, role, text in blocks:
            key = key if own_format else "source:" + key
            ref, original = mapping.get(key), catalog.get(key)
            pieces = [piece.strip() for piece in text.split("\n[…]\n") if piece.strip()]
            if (ref is not None and original and original["role"] == role and pieces
                    and all(normalize(piece) in normalize(original["content"]) for piece in pieces)):
                yield rank, ref, role, text


def score_case(case, candidates, mapping, catalog, framework="remi"):
    question = case["question"]
    blocks = list(visible_blocks(candidates, mapping, catalog, framework))
    ranks, refs = {}, [[] for _ in candidates]
    for rank, ref, role, text in blocks:
        ranks.setdefault(ref, rank)
        if ref not in refs[rank - 1]:
            refs[rank - 1].append(ref)
    gold = question["evidence"]
    hits = sum(ref in ranks for ref in gold)
    facets = [{**facet, "visible": any(ref == facet["ref"] and role == facet["role"]
        and normalize(facet["quote"]).casefold() in normalize(text).casefold()
        for _, ref, role, text in blocks)} for facet in case.get("facets", [])]
    return {"question_id": question["id"], "question_type": question["kind"], "question": question["text"],
        "question_date": question["day"], "gold_evidence": gold, "gold_answer": question["answer"],
        "evidence_ranks": {ref: ranks.get(ref) for ref in gold}, "candidate_turn_refs": refs,
        "evidence_recall": hits / len(gold), "all_evidence_hit": hits == len(gold), "any_evidence_hit": hits > 0,
        "facets": facets, "facet_quote_coverage": sum(item["visible"] for item in facets) / len(facets) if facets else 1,
        "all_facets_visible": all(item["visible"] for item in facets), "scoring": "visible_raw_source_v2",
        "returned_bytes": sum(len(item.get("content", "").encode()) for item in candidates)}


def metrics(results, top_k=10):
    if not results:
        return {"question_count": 0, "mean_evidence_recall": 0, "all_evidence": 0, "any_evidence": 0, "mrr": 0}
    recall, complete, any_hits, reciprocals = [], [], [], []
    for result in results:
        hits = [rank for rank in result["evidence_ranks"].values() if rank is not None and rank <= top_k]
        recall.append(len(hits) / len(result["gold_evidence"]))
        complete.append(len(hits) == len(result["gold_evidence"]))
        any_hits.append(bool(hits))
        reciprocals.append(1 / min(hits) if hits else 0)
    return {"question_count": len(results), "mean_evidence_recall": sum(recall) / len(results),
        "all_evidence": sum(complete) / len(results), "any_evidence": sum(any_hits) / len(results),
        "mrr": sum(reciprocals) / len(results)}


def actions(dataset, batch_turns=8):
    """Use the established causal per-day replay ordering without consulting answers."""
    turns, cursor = dataset["input_turns"], 0
    for case in dataset["cases"]:
        due = defaultdict(list)
        while cursor < len(turns) and turns[cursor]["day"] <= case["question"]["day"]:
            turn = turns[cursor]
            due[turn["day"]].append(turn)
            cursor += 1
        for day, cohort in due.items():
            for start in range(0, len(cohort), batch_turns):
                batch = cohort[start:start + batch_turns]
                first, last = batch[0]["ref"].rsplit(":", 1)[1], batch[-1]["ref"].rsplit(":", 1)[1]
                yield "add", {"request_id": f"rhelm:{dataset['character']}:{day}:turns-{first}-{last}",
                              "session_id": f"rhelm:{dataset['character']}:{day}", "turns": batch}
        yield "search", case


def add_payload(action, owner):
    return {"request_id": action["request_id"], "session_id": action["session_id"], "user_id": owner,
        "messages": [{"role": role, "content": turn[role], "timestamp": turn["timestamp_ms"]}
                     for turn in action["turns"] for role in ("user", "assistant")]}
