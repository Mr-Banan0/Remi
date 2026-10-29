from __future__ import annotations

import hashlib
from copy import deepcopy
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


def command(state, content=None, *, kind="event", quote=None, objects=None, time=None, status=None):
    source = state["new_user_source"]
    return {"kind": kind, "content": content or source["content"],
            "source_id": source["id"], "context_source_ids": [], "quote": quote or source["content"],
            "objects": objects if objects is not None else ([{"kind": "user", "name": "user"}] if re.search(r"\b(I|me|my)\b|我", source["content"], re.I) else []),
            "time_expression": time, "end_expression": None, "status": status or ("occurred" if kind == "event" else "stated"),
            "impact": "unknown"}


def direct_subjects_fixture(state,names,*,aliases=None,kinds=None,force_new=False,pending=()):
    from remi.identity import normalized_name
    from remi.subject_references import current_evidence
    aliases=aliases or {};kinds=kinds or {};source=state['new_user_source']
    fragments=current_evidence(source)
    subjects=[]
    for ref,name in enumerate(names):
        user=name in {'user','@user'};name='@user' if user else name
        kind=kinds.get(name,'person');added=aliases.get(name,[])
        matches=[o for o in state['subject_catalog'] if o['kind']==kind and
            (o['is_user'] if user else normalized_name(name) in {normalized_name(o['name']),*[normalized_name(a['name']) for a in o['aliases']]})]
        if force_new:matches=[]
        uncertain=ref in pending or len(matches)>1
        target=matches[0]['id'] if len(matches)==1 and not uncertain else None
        operation='pending' if uncertain else 'reuse' if target else 'create'
        basis=next((row['id'] for row in state.get('identity_bases',[])
                    if row['target_id']==target),None) if target else None
        fragment=next((row['id'] for row in fragments
                       if normalized_name(name) in normalized_name(row['text'])),fragments[0]['id'])
        reason='ambiguous_identity' if uncertain else 'same_identity' if target else 'canonical_user' if user else 'no_matching_identity'
        subjects.append(dict(ref=ref,operation=operation,reason=reason,target_id=target,
            name=name,kind=kind,aliases=[dict(name=a,source_id=source['id']) for a in added],
            reference=dict(evidence_id=fragment,basis_id=basis if operation=='reuse' else None)))
    return dict(subjects=subjects)



def proposal_fixture(rows,state):
    subjects=state.get('proposal',{}).get('subjects',[])
    def refs(names):
        return [s['ref'] for s in subjects if s['name'] in names or s['name']=='@user' and 'user' in names or set(s['aliases']).intersection(names)]
    return {'items':[{'content':r['content'],'subject_refs':refs(r['names']),'evidence':r['evidence'],
        'record_type':r.get('record_type','fact'),'mode':r.get('mode','asserted'),'context':r.get('context'),
        'status':r.get('status'),'time_expression':r.get('time_expression')} for r in rows],
        'reason':'offline source fixture'}


def subject_decisions_fixture(state):
    return direct_subjects_fixture(state,['user'] if any(word in state['new_user_source']['content'] for word in ['I ','My ','my ','我']) else [])


class ScriptedModel:
    """Test-only fixtures for subject, extraction and memory-maintenance calls."""
    def __init__(self, extract=None, decide=None):
        self.extractor = extract or (lambda state, errors: {'commands': [command(state)]})
        self.decider = decide or (lambda state: {'query_mode': 'direct', 'action_time_expression': None,
                                                  'searches': [], 'relevant_memory_ids': []})
        self.extractions, self.decisions = [], []
        self.oracle_candidates, self.raw_commands = [], []
        self.subject_calls=[]
        self.base_state = None

    def extract_memories(self, state, *, deadline):
        self.base_state = {**state, 'candidates': self.oracle_candidates}
        self.extractions.append((self.base_state, state.get('repair_errors')))
        result = self.extractor(self.base_state, state.get('repair_errors'))
        self.raw_commands = result.get('commands', [])
        plan=proposal_fixture([{'record_type':r['kind'],'mode':r.get('mode','asserted'),'context':r.get('context','personal'),
            'status':r['status'] if r['kind']=='event' else None,'time_expression':r.get('time_expression') if r['kind']=='event' else None,
            'content':r['content'],'names':[o['name'] for o in r.get('objects',[]) if o['kind'] in {'user','person','animal','organization','team'}],
            'evidence':[{'source_id':sid} for sid in [r['source_id'],*r.get('context_source_ids',[])]]} for r in self.raw_commands],state)
        allowed=set(state.get('source_catalog',{}))
        invalid=[e['source_id'] for row in plan['items'] for e in row['evidence'] if e['source_id'] not in allowed]
        if invalid:
            from remi.providers import OutputInvalid
            error=OutputInvalid('extraction_requires_supplied_source')
            error.raw_output=plan
            error.issues=[{'path':['items'],'code':'extraction_requires_supplied_source',
                           'actual':invalid,'allowed_source_ids':sorted(allowed)}]
            raise error
        return plan

    def propose(self, state, *, deadline):
        from remi.memory_maintenance import eligible_targets
        targets=eligible_targets(state)
        text=state.get('new_user_source',{}).get('content','').casefold()
        continuation=bool(re.search(r'\b(that|still|moved|cancelled|canceled|completed|corrected|changed|withdraw|remains|no longer)\b|改到|取消|仍然|还是|不再',text))
        return {'items':[{'ref':row['ref'],
                          'target_id':targets[row['ref']][0] if continuation and len(targets.get(row['ref'],[]))==1 else None,
                          'content':row['content']}
                         for row in state.get('memory_candidates',[])],
                'reason':'offline fixture selects the only eligible recalled target'}

    def maintain_subjects(self,state,*,deadline):
        self.subject_calls.append(deepcopy(state))
        self.base_state={**state,'candidates':self.oracle_candidates}
        result=self.extractor(self.base_state,None)
        commands=result.get('commands',[])
        names=list(dict.fromkeys(o['name'] for row in commands for o in row.get('objects',[]) if o['kind'] in {'user','person','animal','organization','team'}))
        return direct_subjects_fixture(state,names)

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
