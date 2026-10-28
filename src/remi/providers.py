"""GPT-4o-mini for writing/planning, text-embedding-v4 for vectors, Jev for ranking."""
from __future__ import annotations

import json
import math
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Protocol

import httpx
from openai import APIConnectionError, APIStatusError, OpenAI

from .models import Decision, ProposalDraft, SubjectMaintenanceDraft
from .add_judgments import PROPOSE_PROMPT, PROPOSE_EXAMPLES, SUBJECT_PROMPT, SUBJECT_EXAMPLES, PROPOSE_REFERENCES, SUBJECT_REFERENCES
from .retrieval import validate_vector
from .settings import private_key
from .subject_input import render_subject_input


class ProviderUnavailable(RuntimeError):
    pass


class OutputInvalid(ValueError):
    pass


class TextModel(Protocol):
    def propose(self, state: dict, *, deadline: float) -> dict: ...
    def extract_memories(self, state: dict, *, deadline: float) -> dict: ...
    def maintain_subjects(self, state: dict, *, deadline: float) -> dict: ...
    def decide(self, state: dict, *, deadline: float) -> dict: ...


class Embedder(Protocol):
    def embed(self, texts: list[str], *, deadline: float) -> list[list[float]]: ...


@dataclass
class RankResult:
    scores: dict[str, float] = field(default_factory=dict)
    temporal_scores: dict[str, float] = field(default_factory=dict)


class Reranker(Protocol):
    def rank(self, query: str, cards: list[dict], *, deadline: float) -> RankResult: ...


DECISION_PROMPT = """Plan at most two supplemental memory searches. Return one JSON object in exactly this shape:
{
  "query_mode": "direct",
  "action_time_expression": null,
  "searches": [{
    "role": "direct",
    "query": "focused retrieval target",
    "time_expression": null,
    "anchor_source_id": null,
    "anchor_step": null,
    "relation": "independent"
  }],
  "relevant_memory_ids": []
}

Allowed query_mode values: direct, verify_premise, action, temporal_join, current_state.
Allowed role values: direct, premise_check, constraint, temporal_anchor, answer_detail, latest_state.
Allowed relation values: independent, same_day, before, after.
anchor_step is JSON number 0 for a second search anchored to the first search; otherwise it is JSON null.

Classify query_mode first. The program supplies mechanical retrieval fields after this decision.
- direct: searches may be empty when the visible evidence already answers the question.
- verify_premise: include premise_check. Preserve stable people, place and explicit date anchors; ask what actually happened without treating the asserted event or outcome as true.
- action: include constraint and latest_state. Translate the action into abilities, risks, practical limits and commitments; retrieve conditions that affect feasibility and later evidence showing whether they still apply.
- temporal_join: include temporal_anchor followed by answer_detail. The second search uses anchor_step=0 and the requested before, after or same_day relation.
- current_state: include latest_state; use the second search for a condition or answer detail needed to interpret that state.

Write retrieval targets, not answers and not copied evidence sentences. A medical or practical restriction can matter without repeating the action words. A dated activity can correct a false premise. For an incident-anchored question, identify the incident before searching for its consequence or requested detail.

time_expression uses only a date phrase supported by the original question. action_time_expression holds the date of a newly requested action and does not restrict historical evidence. reference_time resolves relative phrases and creates no restriction by itself. anchor_source_id uses only an input source ID. anchor_step=0 refers to the first search; then time_expression and anchor_source_id are null. relation is independent when no temporal anchor is used.

Memory cards contain independent propositions or occurrences. Names support navigation; original evidence establishes roles and direction. Select relevant_memory_ids only when a visible current condition is useful. When repair_errors and previous_output are present, return a complete corrected object that satisfies this contract.
"""


DECISION_EXAMPLES = [
    (
        {"query": "I remember giving a budget lecture at Harbor School on May 4. How did students respond?",
         "reference_time": "2028-06-01T00:00:00+00:00", "evidence": [], "current_conditions": []},
        {"query_mode": "verify_premise", "action_time_expression": None,
         "searches": [{"role": "premise_check",
             "query": "Actual activities involving the user at Harbor School on May 4",
             "time_expression": "May 4", "anchor_source_id": None, "anchor_step": None,
             "relation": "same_day"}], "relevant_memory_ids": []},
    ),
    (
        {"query": "Should I resume running tomorrow?", "reference_time": "2028-06-01T00:00:00+00:00",
         "evidence": [], "current_conditions": []},
        {"query_mode": "action", "action_time_expression": "tomorrow", "searches": [
            {"role": "constraint", "query": "Conditions affecting the user's ability and safety when running",
             "time_expression": None, "anchor_source_id": None, "anchor_step": None,
             "relation": "independent"},
            {"role": "latest_state", "query": "Later evidence showing whether those running limitations still apply",
             "time_expression": None, "anchor_source_id": None, "anchor_step": None,
             "relation": "independent"}], "relevant_memory_ids": []},
    ),
    (
        {"query": "What device did I use after my phone failed?",
         "reference_time": "2028-06-01T00:00:00+00:00", "evidence": [], "current_conditions": []},
        {"query_mode": "temporal_join", "action_time_expression": None, "searches": [
            {"role": "temporal_anchor", "query": "The user's phone failure incident and its occurrence time",
             "time_expression": None, "anchor_source_id": None, "anchor_step": None,
             "relation": "independent"},
            {"role": "answer_detail", "query": "The device the user used following that phone failure",
             "time_expression": None, "anchor_source_id": None, "anchor_step": 0,
             "relation": "after"}], "relevant_memory_ids": []},
    ),
]

SUPPORT_QUESTION = (
    "How much does `evidence` contribute to answering `query`? "
    "Evaluate every item in evidence.retrieval_objectives as a focused reason this evidence was retrieved. "
    "A direct objective seeks an answer fact; premise_check seeks evidence that confirms or corrects the "
    "question's premise; constraint seeks a condition affecting suitability or feasibility; temporal_anchor "
    "and identity anchors establish the event, time or participant needed for an answer; answer_detail seeks "
    "the requested detail; latest_state seeks the currently applicable state. "
    "Score usefulness for the original answer, not lexical similarity. Judge original source words, speakers, "
    "status and dates. A medical or practical restriction can be strong action evidence without repeating the "
    "action words. A dated actual activity can strongly correct a false premise. An identity or time anchor can "
    "be strong evidence when the answer depends on it. Shared topic alone, another occurrence and unrelated "
    "generic advice provide little support."
)

TEMPORAL_MATCH_QUESTION = (
    "Does the original text in `target` support a specific instance of the activity and participants "
    "requested by `target.query`, with a status appropriate to the original `query`? "
    "Treat a shared person/topic, a different activity, and a denied occurrence as insufficient. "
    "Use source words and speakers; the memory summary is a claim to check against the original."
)


def strict_schema(model):
    schema = model.model_json_schema()
    def visit(value):
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["required"] = list(value.get("properties", {}))
                value["additionalProperties"] = False
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(schema)
    return schema


def scoped_schema(model, payload):
    """Bound stored targets and sources; subject refs are created by the direct writer."""
    schema=strict_schema(model)
    if 'new_user_source' not in payload:return schema
    sources=[payload['new_user_source'],*payload.get('context_sources',[])]
    for row in payload.get('history',[]):sources.extend(row.get('sources',[]))
    source_ids=sorted({source['id'] for source in sources})
    entities=[o['id'] for o in payload.get('subject_catalog',[])]
    relations=[r['id'] for r in payload.get('relation_catalog',[])]
    refs=[s['ref'] for s in payload.get('resolved_subjects',[])]
    def optional_ids(ids):return {'anyOf':[{'type':'string','enum':ids},{'type':'null'}]} if ids else {'type':'null'}
    for name,definition in schema.get('$defs',{}).items():
        props=definition.get('properties',{})
        if name in {'SourceCitation','EvidenceRef','SubjectNameDraft'}:props['source_id']['enum']=source_ids
        if name=='SubjectDraft':props['target_id']=optional_ids(entities)
        elif name=='ConnectionChangeDraft':
            props['target_id']=optional_ids(relations)
            props['source_id']['enum']=[payload['new_user_source']['id']]
        if name=='CandidateDraft':
            allowed=refs
            if allowed:props['subject_refs']['items']['enum']=allowed
            else:props['subject_refs']['maxItems']=0
    return schema


def remaining(deadline: float, maximum: float) -> float:
    value = min(maximum, deadline - monotonic())
    if value <= 0:
        raise ProviderUnavailable("processing_deadline")
    return value


def error_label(error):
    status = getattr(error, "status_code", None)
    if status is None and getattr(error, "response", None) is not None:
        status = error.response.status_code
    return {"error_type": type(error).__name__, "http_status": status}


def transient(error):
    if isinstance(error, APIStatusError):
        return error.status_code in {408, 429} or error.status_code >= 500
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 429} or error.response.status_code >= 500
    return isinstance(error, (APIConnectionError, httpx.TransportError))


def semantic_references(payload):
    """Short source/card IDs stay separate from candidate IDs and literal source text."""
    aliases, reverse, counts = {}, {}, {}
    candidate_ids = {item['id'] for key in ['items','connections'] for item in payload.get('proposal', {}).get(key, [])}
    def register(value, kind):
        if value in aliases:
            return
        counts[kind] = counts.get(kind, 0) + 1
        alias = kind + str(counts[kind])
        while alias in candidate_ids:
            counts[kind] += 1
            alias = kind + str(counts[kind])
        aliases[value], reverse[alias] = alias, value
    for entity in payload.get('subject_catalog',[]):
        register(entity['id'],'E')
    for subject in payload.get('resolved_subjects',[]):
        if subject.get('id'):register(subject['id'],'E')
    for card in [*payload.get('history', []),*payload.get('relation_catalog',[])]:
        register(card['id'], 'M')
    def collect(value):
        if isinstance(value, dict):
            if 'id' in value and 'role' in value and 'content' in value:
                register(value['id'], 'S')
            if isinstance(value.get('owner'), str):
                register(value['owner'], 'U')
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(payload)
    def translate(value, mapping, key=None):
        if isinstance(value, dict):
            candidate = ('evidence' in value and 'content' in value) or ('subject_refs' in value and 'id' in value)
            return {name: item if candidate and name == 'id' else translate(item, mapping, name)
                    for name, item in value.items()}
        if isinstance(value, list):
            return [translate(item, mapping, key) for item in value]
        if isinstance(value, str) and key in {'id', 'owner', 'source_id', 'source_ids', 'target_id','existing_id','entity_ids','endpoint_ids','ordered_subject_ids'}:
            return mapping.get(value, value)
        return value
    return translate(payload, aliases), lambda value: translate(value, reverse)


class GPTModel:
    def __init__(self, api_key, base_url, model="gpt-4o-mini", timeout=45, client=None):
        if model != "gpt-4o-mini":
            raise ValueError("this_architecture_uses_gpt_4o_mini")
        self.model, self.timeout = model, timeout
        self.client = client or OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout)
        self.calls = deque(maxlen=2048)

    def _json(self, prompt, state, schema, kind, deadline):
        def evidence_input(value):
            if isinstance(value, dict):
                if kind.startswith('add_') and not kind.startswith('add_subjects') and set(value) == {'source_id', 'quote'}:
                    return {'source_id': value['source_id']}
                return {key: evidence_input(item) for key, item in value.items() if key not in ({"created_at", "owner", "revision_id", "target_revision_id", "source_catalog"} if kind.startswith("add_") else {"created_at"})}
            if isinstance(value, list):
                return [evidence_input(item) for item in value]
            return value
        payload = evidence_input(state)
        if kind.startswith('add_subjects'):
            payload={k:payload[k] for k in ['new_user_source','context_sources','subject_catalog','relation_catalog','history',
                'accepted_subjects','accepted_connections','rejected_subjects','rejected_connections','repair_errors'] if k in payload}
        elif kind.startswith('add_propose'):
            payload={k:payload[k] for k in ['new_user_source','context_sources','resolved_subjects','history','repair_errors'] if k in payload}
        if "new_user_source" in state:
            primary = state["new_user_source"]
            payload["source_reference_time"] = primary.get("message_at") or primary.get("created_at")
        restore = lambda value: value
        if kind.startswith("add_"):
            payload, restore = semantic_references(payload)
            references = SUBJECT_REFERENCES if kind.startswith('add_subjects') else PROPOSE_REFERENCES
            prompt += "\nReference protocol: " + references

        body = render_subject_input(payload) if kind.startswith('add_subjects') else json.dumps(payload, ensure_ascii=False)
        messages=[{'role':'system','content':prompt}]
        examples=(PROPOSE_EXAMPLES if kind.startswith('add_propose') else
                  SUBJECT_EXAMPLES if kind.startswith('add_subjects') else
                  DECISION_EXAMPLES if kind.startswith('search_decision') else [])
        for example_input,example_output in examples:
            messages.extend([{'role':'user','content':render_subject_input(example_input) if kind.startswith('add_subjects') else json.dumps(example_input,ensure_ascii=False)},
                             {'role':'assistant','content':json.dumps(example_output,ensure_ascii=False)}])
        messages.append({'role':'user','content':body})
        for attempt in range(2):
            started = monotonic()
            trace = {"kind": kind, "model": self.model, "attempt": attempt + 1}
            try:
                response = self.client.chat.completions.create(
                    model=self.model, temperature=0, max_completion_tokens=6000,
                    timeout=remaining(deadline, self.timeout),
                    messages=messages,
                    response_format={"type": "json_schema", "json_schema": {
                        "name": schema.__name__, "strict": True, "schema": scoped_schema(schema, payload)}})
                if response.usage:
                    trace.update(input_tokens=response.usage.prompt_tokens, output_tokens=response.usage.completion_tokens)
                trace.update(input_bytes=len(body.encode()), output_bytes=len((response.choices[0].message.content or '').encode()))
                message = response.choices[0].message
                if getattr(message, "refusal", None) or not message.content:
                    raise OutputInvalid("model_result_unavailable")
                result = json.loads(message.content)
                if not isinstance(result, dict):
                    raise OutputInvalid("model_result_requires_object")
                return restore(result)
            except (json.JSONDecodeError, OutputInvalid) as error:
                trace.update(error_label(error))
                raise OutputInvalid("model_json_invalid") from error
            except Exception as error:
                trace.update(error_label(error))
                if attempt == 0 and transient(error) and deadline - monotonic() > 1:
                    sleep(.2)
                    continue
                raise ProviderUnavailable("text_provider_unavailable") from error
            finally:
                trace["seconds"] = monotonic() - started
                self.calls.append(trace)
        raise ProviderUnavailable("text_provider_unavailable")

    def propose(self, state, *, deadline):
        return self._json(PROPOSE_PROMPT + ("\nCorrect repair_errors and return the candidate plan with source IDs from this input." if state.get("repair_errors") else ""), state, ProposalDraft,
                          "add_propose_repair" if state.get("repair_errors") else "add_propose", deadline)

    def maintain_subjects(self,state,*,deadline):
        return self._json(SUBJECT_PROMPT,state,SubjectMaintenanceDraft,
            'add_subjects_repair' if state.get('repair_errors') else 'add_subjects',deadline)

    def decide(self, state, *, deadline):
        repair = bool(state.get("repair_errors"))
        prompt = DECISION_PROMPT + ("\nCorrect every repair error and return the complete decision." if repair else "")
        return self._json(prompt, state, Decision, "search_decision_repair" if repair else "search_decision", deadline)

    def close(self):
        self.client.close()


class DashScopeEmbedder:
    def __init__(self, api_key, base_url, dimensions=1024, client=None):
        self.dimensions = dimensions
        self.client = client or OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=30)
        self.calls = deque(maxlen=2048)

    def embed(self, texts, *, deadline):
        vectors = []
        for offset in range(0, len(texts), 10):
            batch = texts[offset:offset + 10]
            started = monotonic()
            trace = {"kind": "embedding", "model": "text-embedding-v4", "batch_size": len(batch)}
            try:
                response = self.client.embeddings.create(model="text-embedding-v4", input=batch,
                                                         dimensions=self.dimensions, timeout=remaining(deadline, 30))
                ordered = sorted(response.data, key=lambda item: item.index)
                if [item.index for item in ordered] != list(range(len(batch))):
                    raise OutputInvalid("embedding_indices_invalid")
                for item in ordered:
                    validate_vector(item.embedding, self.dimensions)
                    vectors.append(item.embedding)
            except Exception as error:
                trace.update(error_label(error))
                raise ProviderUnavailable("embedding_provider_unavailable") from error
            finally:
                trace["seconds"] = monotonic() - started
                self.calls.append(trace)
        return vectors

    def close(self):
        self.client.close()


class JevReranker:
    """The verified gateway's native System One protocol, restricted to evidence ranking."""
    def __init__(self, api_key, base_url="https://api.quickrouter.ai/v1", model="jev-1.13.0", timeout=8, transport=None):
        url = httpx.URL(base_url)
        if url.scheme not in {"https", "http"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("reranker_base_url_invalid")
        self.model, self.timeout = model, timeout
        self.client = httpx.Client(base_url=base_url.rstrip("/") + "/", transport=transport,
                                  headers={"Authorization": "Bearer " + api_key}, timeout=timeout)
        self.calls = deque(maxlen=2048)

    def rank(self, query, cards, *, deadline):
        batches = [cards[offset:offset + 8] for offset in range(0, len(cards), 8)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._batch, query, batch, deadline) for batch in batches]
            result = RankResult()
            for future in futures:
                batch = future.result()
                result.scores.update(batch.scores)
                result.temporal_scores.update(batch.temporal_scores)
            return result

    def _batch(self, query, cards, deadline):
        questions = {card["id"]: {"type": "noul", "instructions": {"evidence": card, "question": SUPPORT_QUESTION},
            "criteria": {"true": "Strong contribution to the original answer under a matched retrieval objective.",
                         "false": "Lexical overlap or shared topic without useful answer evidence."}} for card in cards}
        for card in cards:
            for target in card.get("temporal_targets", []):
                questions[target["id"]] = {"type": "noul", "instructions": {"target": target,
                    "question": TEMPORAL_MATCH_QUESTION}, "criteria": {
                        "true": "The source supports the requested activity, participants and applicable status.",
                        "false": "Different activity, shared topic only, denied occurrence or unsupported summary."}}
        for attempt in range(2):
            started = monotonic()
            trace = {"kind": "rerank", "model": self.model, "batch_size": len(cards), "attempt": attempt + 1}
            try:
                response = self.client.post("systemone", json={"model": self.model, "state": {"query": query}, "questions": questions},
                                            timeout=remaining(deadline, self.timeout))
                trace["http_status"] = response.status_code
                response.raise_for_status()
                body = response.json()
                answers = body.get("answers", {}) if isinstance(body, dict) else {}
                if not isinstance(answers, dict):
                    raise OutputInvalid("reranker_answers_invalid")
                result = RankResult()
                temporal_ids = {target["id"] for card in cards for target in card.get("temporal_targets", [])}
                for key in questions:
                    answer = answers.get(key, {})
                    value = answer.get("noul") if isinstance(answer, dict) else None
                    if (isinstance(answer, dict) and answer.get("type") == "noul" and not isinstance(value, bool)
                            and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1):
                        (result.temporal_scores if key in temporal_ids else result.scores)[key] = value
                return result
            except Exception as error:
                trace.update(error_label(error))
                if attempt == 0 and transient(error) and deadline - monotonic() > .25:
                    sleep(.05)
                    continue
                return RankResult()
            finally:
                trace["seconds"] = monotonic() - started
                self.calls.append(trace)
        return RankResult()

    def close(self):
        self.client.close()


def providers_from_environment():
    text_key = private_key("LLM_API_KEY", "LLM_API_KEY_FILE")
    embedding_key = private_key("DASHSCOPE_API_KEY", "DASHSCOPE_API_KEY_FILE")
    reranker_key = private_key("TYPESAFE_API_KEY", "TYPESAFE_API_KEY_FILE")
    missing = [name for name, value in (("LLM_API_KEY", text_key), ("DASHSCOPE_API_KEY", embedding_key),
                                       ("TYPESAFE_API_KEY", reranker_key)) if not value]
    if missing:
        raise RuntimeError("missing_credentials: " + ", ".join(missing))
    model = GPTModel(text_key, os.getenv("LLM_BASE_URL", "https://api.quickrouter.us/v1"),
                     os.getenv("LLM_MODEL", "gpt-4o-mini"), float(os.getenv("REMI_LLM_TIMEOUT", "45")))
    embedder = DashScopeEmbedder(embedding_key, os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                                int(os.getenv("REMI_EMBEDDING_DIMENSIONS", "1024")))
    ranker = JevReranker(reranker_key, os.getenv("TYPESAFE_BASE_URL", "https://api.quickrouter.ai/v1"),
                         os.getenv("TYPESAFE_MODEL", "jev-1.13.0"), float(os.getenv("REMI_RERANK_TIMEOUT", "8")))
    return model, embedder, ranker
