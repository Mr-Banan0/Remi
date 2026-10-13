from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from remi.models import AddMessage, AddRequest, SearchRequest
from remi.retrieval import tokenize
from remi.engine import MemoryEngine as CoreEngine


def request(text, *, owner="u1", request_id="r1", when="2024-06-02T10:00:00+00:00", assistant=None):
    stamp = int(datetime.fromisoformat(when).timestamp() * 1000) if when else None
    messages = [AddMessage(role="user", content=text, timestamp=stamp)]
    if assistant:
        messages.append(AddMessage(role="assistant", content=assistant, timestamp=stamp))
    return AddRequest(user_id=owner, request_id=request_id, session_id="s1", messages=messages)


def command(state, content=None, *, kind="event", operation="create", target=None, quote=None, objects=None, time=None, status=None):
    source = state["new_user_source"]
    return {"operation": operation, "target_id": target, "kind": kind, "content": content or source["content"],
            "source_id": source["id"], "context_source_ids": [], "quote": quote or source["content"],
            "objects": objects if objects is not None else [{"kind": "user", "name": "self"}],
            "time_expression": time, "end_expression": None, "status": status or ("occurred" if kind == "event" else "stated"),
            "impact": "unknown"}


class ScriptedModel:
    def __init__(self, extract=None, decide=None):
        self.extractor = extract or (lambda state, errors: {"commands": [command(state)]})
        self.decider = decide or (lambda state: {"steps": [], "relevant_memory_ids": []})
        self.extractions, self.decisions = [], []
        self.oracle_candidates = []
        self.oracle_commands = {}
        self.oracle_state, self.oracle_plan = None, None
        self.judger = ScriptedJudger(self)

    def fixture(self, state, errors=None):
        self.extractions.append((state, errors))
        result = self.extractor({**state, "candidates": self.oracle_candidates}, errors)
        return result

    def split(self, state, *, deadline):
        return {"items": [{"content": value["content"] + (" " + value["quote"]
                         if value.get("quote") and value["quote"] != value["content"] else "")}
                          for value in self.oracle_plan["commands"]]}

    def materialize(self, state, *, deadline):
        if state.get("repair_errors"):
            failures = [{"position": int(item["item_id"][1:]), "error": item["error"]}
                        for item in state["repair_errors"] if item.get("item_id")]
            result = self.fixture(self.oracle_state, failures)
            values = ({f"i{item['position']}": item["command"] for item in result["replacements"]}
                      if isinstance(result, dict) and "replacements" in result else
                      {f"i{index}": value for index, value in enumerate(result.get("commands", []))}
                      if isinstance(result, dict) else {})
        elif isinstance(self.oracle_plan, Exception):
            from remi.providers import OutputInvalid
            if isinstance(self.oracle_plan, OutputInvalid):
                raise self.oracle_plan
            result = self.extractor({**self.oracle_state, "candidates": self.oracle_candidates}, None)
            values = {f"i{index}": value for index, value in enumerate(result.get("commands", []))}
        else:
            values = self.oracle_commands
        items = []
        for key, raw in values.items():
            if not isinstance(raw, dict):
                items.append(raw)
                continue
            value = {name: raw[name] for name in ("content", "objects", "time_expression", "end_expression", "status", "impact")}
            value.update({name: value for name, value in raw.items() if name not in {
                "operation", "target_id", "kind", "source_id", "context_source_ids", "quote",
                "content", "objects", "time_expression", "end_expression", "status", "impact"}})
            if raw.get("source_id") != self.oracle_state["new_user_source"]["id"]:
                value["source_id"] = raw.get("source_id")
            if raw.get("operation") == "create" and raw.get("target_id") not in (None, "null", "none", ""):
                value["target_id"] = raw["target_id"]
            items.append({"item_id": key, **value})
        return {"items": items}

    def decide(self, state, *, deadline):
        self.decisions.append(state)
        return self.decider(state)


def answer(choice):
    return {"choice": choice, "confidence": 1., "probabilities": {choice: 1.}}


class ScriptedJudger:
    """Test oracle maps existing fixture intent to the new typed provider protocol."""
    def __init__(self, model):
        self.model = model
        self.calls = []

    def route(self, state, *, deadline):
        from remi.providers import OutputInvalid, ProviderUnavailable
        self.model.oracle_state = state
        try:
            self.model.oracle_plan = self.model.fixture(state)
        except (OutputInvalid, ProviderUnavailable) as error:
            self.model.oracle_plan = error
            self.model.oracle_commands = {"i0": command(state)}
            return {"route": answer("single"), "category": answer("event")}
        raws = self.model.oracle_plan.get("commands", []) if isinstance(self.model.oracle_plan, dict) else []
        self.model.oracle_commands = {f"i{index}": raw for index, raw in enumerate(raws)}
        contexts = raws[0].get("context_source_ids", []) if len(raws) == 1 and isinstance(raws[0], dict) else []
        return {"route": answer("multiple" if len(raws) > 1 else "single" if raws else "raw_only"),
                "category": answer(raws[0]["kind"] if raws and isinstance(raws[0], dict) else "uncertain"),
                **{f"i0_context_{index}": {"noul": float(source["id"] in contexts)}
                   for index, source in enumerate(state["context_sources"])}}

    def classify(self, states, *, deadline):
        result = {"split_coverage": {"noul": 1.}}
        for state in states:
            key = state["item_id"]
            result[key] = answer(self.model.oracle_commands[key]["kind"])
            result[key + "_grounding"] = {"noul": 1.}
            for index, source in enumerate(state["context_sources"]):
                result[f"{key}_context_{index}"] = {"noul": float(source["id"] in self.model.oracle_commands[key].get("context_source_ids", []))}
        return result

    def select(self, states, *, deadline):
        result = {}
        for state in states:
            key = state["item_id"]
            raw = self.model.oracle_commands[key]
            target = next((candidate for candidate in state["candidates"] if candidate["id"] == raw.get("target_id")), None)
            handle = target["handle"] if target else "new" if raw.get("operation") == "create" else "uncertain"
            result[key + "_target"] = answer(handle)
            result[key + "_new"] = answer("create")
            for candidate in state["candidates"]:
                result[key + "_" + candidate["handle"]] = answer(raw["operation"] if candidate is target else "different")
        return result


class TestEngine(CoreEngine):
    """Current production engine with a test-only command-fixture oracle."""
    __test__ = False

    def add(self, request):
        if isinstance(self.model, ScriptedModel):
            self.model.oracle_candidates = [
                {"id": memory.id, "kind": memory.kind, "content": memory.current.content,
                 "source_ids": memory.current.source_ids, "status": memory.current.status,
                 "event_at": memory.current.event_at, "valid_from": memory.current.valid_from,
                 "valid_to": memory.current.valid_to} for memory in self.store.memories(request.user_id)]
        return super().add(request)


class HashEmbedder:
    """Test-only deterministic lexical vectors; no claims of semantic model accuracy."""
    def __init__(self):
        self.calls = []

    def embed(self, texts, *, deadline):
        self.calls.append(texts)
        result = []
        for text in texts:
            vector = [0.0] * 32
            for token in tokenize(text):
                vector[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 32] += 1
            vector[0] += .01
            result.append(vector)
        return result


def query(text, owner="u1", top_k=10):
    return SearchRequest(query=text, user_id=owner, top_k=top_k)
