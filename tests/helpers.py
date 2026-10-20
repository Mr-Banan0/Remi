from __future__ import annotations

import hashlib
import re
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
            "objects": objects if objects is not None else ([{"kind": "user", "name": "self"}] if re.search(r"\b(I|me|my)\b|我", source["content"], re.I) else []),
            "time_expression": time, "end_expression": None, "status": status or ("occurred" if kind == "event" else "stated"),
            "impact": "unknown"}


class ScriptedModel:
    """Test-only intent fixtures expressed through the current local-maintenance contract."""
    def __init__(self, extract=None, decide=None):
        self.extractor = extract or (lambda state, errors: {'commands': [command(state)]})
        self.decider = decide or (lambda state: {'steps': [], 'relevant_memory_ids': []})
        self.extractions, self.decisions, self.maintenances = [], [], []
        self.oracle_candidates, self.raw_commands = [], []
        self.base_state = None

    def propose(self, state, *, deadline):
        self.base_state = {**state, 'candidates': self.oracle_candidates}
        self.extractions.append((self.base_state, state.get('repair_errors')))
        result = self.extractor(self.base_state, state.get('repair_errors'))
        self.raw_commands = result.get('commands', [])
        return {'items': [{'content': raw['content'],
            'mentions': [{'name': obj['name']} for obj in raw.get('objects', [])],
            'evidence': [{'source_id': sid} for sid in [raw['source_id'], *raw.get('context_source_ids', [])]]}
            for i, raw in enumerate(self.raw_commands)], 'reason': 'offline intent fixture'}

    def maintain(self, state, *, deadline):
        self.maintenances.append(state)
        if state.get('repair_errors'):
            self.extractions.append((self.base_state, state['repair_errors']))
            result = self.extractor(self.base_state, state['repair_errors'])
            self.raw_commands = result.get('commands', self.raw_commands)
        decisions = []
        for candidate in state['proposal']['items']:
            i = int(candidate['id'])
            raw = self.raw_commands[i]
            operation = raw['operation']
            kind = 'relation' if raw['kind'] in {'fact','preference','experience'} else 'event'
            target = raw['target_id']
            change = None
            if operation == 'update':
                operation = 'patch_event'
                change = {'planned': 'reschedule', 'occurred': 'complete', 'cancelled': 'cancel'}.get(raw['status'], 'complete')
            decision = {'candidate_id': candidate['id'], 'operation': operation, 'target_id': target,
                'record_type': kind if operation == 'create' else None,
                'content': raw['content'] if operation == 'create' else None,
                'mode': raw.get('mode', 'asserted') if operation == 'create' else None,
                'context': raw.get('context', 'personal') if operation == 'create' else None,
                'status': raw['status'] if kind == 'event' and operation == 'create' else None,
                'event_change': change,
                'time_expression': raw.get('time_expression') if kind == 'event' else None,
                'reason': 'offline intent fixture'}
            decisions.append(decision)
        return {'decisions': decisions}

    def decide(self, state, *, deadline):
        self.decisions.append(state)
        return self.decider(state)


class TestEngine(CoreEngine):
    __test__ = False
    def add(self, request):
        if isinstance(self.model, ScriptedModel):
            self.model.oracle_candidates = [{'id': m.id, 'kind': m.tags[0], 'tags': m.tags,
                'content': m.current.content, 'source_ids': m.current.source_ids, 'status': m.current.status,
                'event_at': m.current.event_at, 'valid_from': None, 'valid_to': None}
                for m in self.store.memories(request.user_id)]
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
