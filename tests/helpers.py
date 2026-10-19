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
    """Translate historical test intents into the current two-stage fixture contract."""
    def __init__(self, extract=None, decide=None):
        self.extractor = extract or (lambda state, errors: {"commands": [command(state)]})
        self.decider = decide or (lambda state: {"steps": [], "relevant_memory_ids": []})
        self.extractions, self.decisions = [], []
        self.oracle_candidates = []
        self.raw_commands = []
        self.base_state = None

    def propose(self, state, *, deadline):
        self.base_state = {**state, 'candidates': self.oracle_candidates}
        errors = state.get('repair_errors')
        self.extractions.append((self.base_state, errors))
        result = self.extractor(self.base_state, errors)
        self.raw_commands = result.get('commands', []) if isinstance(result, dict) else []
        return {'items': [{'id': f'i{i}', 'content': raw['content'],
            'evidence': [{'source_id': state['new_user_source']['id'], 'quote': state['new_user_source']['content']}],
            'entities': [{'name': obj['name'], 'kind': obj['kind']} for obj in raw.get('objects', [])],
            'tags': [raw['kind']]} for i, raw in enumerate(self.raw_commands)], 'reason': 'fixture'}

    def maintain(self, state, *, deadline):
        if state.get('repair_errors'):
            errors = [{'position': i, 'error': state['repair_errors'][0]['error']} for i in range(len(self.raw_commands))]
            self.extractions.append((self.base_state, errors))
            result = self.extractor(self.base_state, errors)
            if 'replacements' in result:
                for item in result['replacements']:
                    self.raw_commands[item['position']] = item['command']
            else:
                self.raw_commands = result.get('commands', [])
        entities, edits = {}, []
        for i, raw in enumerate(self.raw_commands):
            roles = []
            evidence = [{'source_id': raw['source_id'], 'quote': raw['quote']}]
            for sid in raw.get('context_source_ids', []):
                source = next((s for s in state['context_sources'] if s['id'] == sid), None)
                if source:
                    evidence.append({'source_id': sid, 'quote': source['content']})
            for obj in raw.get('objects', []):
                ref = obj['kind'] + ':' + obj['name']
                self_ref = obj['kind'] == 'user' and obj['name'] == 'self'
                target = next((e['id'] for e in state['candidate_entities'] if e['is_self'] == self_ref and
                    (self_ref or e['name'] == obj['name'] or obj['name'] in e['aliases'])), None)
                entities[ref] = {'ref': ref, 'target_id': target, 'kind': 'person' if self_ref else obj['kind'],
                    'name': 'self' if self_ref else obj['name'], 'is_self': self_ref, 'aliases': [],
                    'evidence': [{'source_id': s['id'], 'quote': s['content']} for s in [state['new_user_source'], *state['context_sources']]]}
                roles.append({'entity_ref': ref, 'role': 'subject' if self_ref else 'participant'})
            edit = {'ref': f'm{i}', 'candidate_ids': [f'i{i}'], 'operation': 'write' if raw['operation'] in {'create','update'} else raw['operation'],
                'target_id': raw['target_id'], 'content': raw['content'], 'change': raw.get('change', 'replace' if raw['operation'] in {'create','retract'} else 'supplement'), 'tags': [raw['kind']], 'evidence': evidence,
                'roles': roles, 'assertions': [], 'links': [], 'time_expression': raw.get('time_expression'),
                'end_expression': raw.get('end_expression'), 'status': raw['status'],
                'corrects_occurrence': raw.get('corrects_occurrence', False),
                'temporal_scope': 'ongoing' if raw['kind'] in {'fact', 'preference'} else 'episodic', 'impact': raw['impact']}
            edits.append(edit)
        return {'entities': list(entities.values()), 'edits': edits, 'discarded': []}

    def decide(self, state, *, deadline):
        self.decisions.append(state)
        return self.decider(state)


class TestEngine(CoreEngine):
    """Production engine with a test-only intent fixture adapter."""
    __test__ = False

    def add(self, request):
        if isinstance(self.model, ScriptedModel):
            self.model.oracle_candidates = [
                {'id': m.id, 'kind': m.tags[0], 'tags': m.tags, 'content': m.current.content,
                 'source_ids': m.current.source_ids, 'status': m.current.status,
                 'event_at': m.current.event_at, 'valid_from': m.current.valid_from,
                 'valid_to': m.current.valid_to} for m in self.store.memories(request.user_id)]
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
